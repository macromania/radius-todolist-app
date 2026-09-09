#!/usr/bin/env python3
"""Issue one gateway certificate from its own scoped in-cluster identity."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def persist_account(configuration: Path, secrets, secret_name: str) -> None:
    stored = {
        str(path.relative_to(configuration)): base64.b64encode(path.read_bytes()).decode()
        for path in (configuration / "accounts").rglob("*")
        if path.is_file()
    }
    if not stored:
        return
    state = json.dumps(stored, separators=(",", ":"))
    if len(state.encode()) > 24000:
        raise ValueError("ACME account state exceeds the vault secret size")
    secrets.set_secret(secret_name, state)


def issue_in_cluster(args) -> str | None:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import WorkloadIdentityCredential
    from azure.keyvault.certificates import CertificateClient
    from azure.keyvault.secrets import SecretClient

    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.cloudapp\.azure\.com", args.domain):
        raise ValueError("Only a provisioned Azure DNS name is accepted")
    if (
        args.certificate_name != f"gateway-{args.slot}"
        or args.account_secret != f"acme-{args.slot}"
    ):
        raise ValueError("Certificate names must match the plane allocation")
    vault_url = f"https://{args.vault_name}.vault.azure.net"
    credential = WorkloadIdentityCredential()
    certificates = CertificateClient(vault_url, credential)
    secrets = SecretClient(vault_url, credential)
    try:
        try:
            existing = certificates.get_certificate(args.certificate_name)
        except ResourceNotFoundError:
            existing = None
        if (
            existing
            and not args.force
            and not args.staging
            and (existing.properties.tags or {}).get("acmeEnvironment") == "production"
            and (existing.properties.tags or {}).get("hostname") == args.domain
        ):
            expires = existing.properties.expires_on
            if expires and expires > datetime.now(UTC) + timedelta(days=7):
                return f"{vault_url}/secrets/{args.certificate_name}"
        with tempfile.TemporaryDirectory(prefix="radplanes-acme-") as temporary:
            root = Path(temporary)
            configuration = root / "config"
            configuration.mkdir(mode=0o700)
            try:
                account = secrets.get_secret(args.account_secret).value
            except ResourceNotFoundError:
                account = None
            if account:
                for relative, content in json.loads(account).items():
                    path = configuration / relative
                    if not path.resolve().is_relative_to(configuration) or not relative.startswith(
                        "accounts/"
                    ):
                        raise ValueError("Stored ACME account contains an invalid path")
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path.write_bytes(base64.b64decode(content, validate=True))
                    path.chmod(0o600)
            environment = {
                **os.environ,
                "ACME_DOMAIN": args.domain,
                "ACME_NAMESPACE": args.namespace,
                "ACME_CONFIGMAP": "acme-challenges",
            }
            command = [
                "certbot",
                "certonly",
                "--manual",
                "--preferred-challenges",
                "http",
                "--manual-auth-hook",
                "python /app/operations/acme-hook.py present",
                "--manual-cleanup-hook",
                "python /app/operations/acme-hook.py cleanup",
                "--non-interactive",
                "--agree-tos",
                "--register-unsafely-without-email",
                "--config-dir",
                str(configuration),
                "--work-dir",
                str(root / "work"),
                "--logs-dir",
                str(root / "logs"),
                "--domain",
                args.domain,
                "--cert-name",
                args.certificate_name,
            ]
            if args.staging:
                command.append("--staging")
            try:
                subprocess.run(command, env=environment, check=True, timeout=600)
            finally:
                persist_account(configuration, secrets, args.account_secret)
            if args.staging:
                return None
            live = configuration / "live" / args.certificate_name
            pfx = root / "certificate.pfx"
            subprocess.run(
                [
                    "openssl",
                    "pkcs12",
                    "-export",
                    "-out",
                    str(pfx),
                    "-inkey",
                    str(live / "privkey.pem"),
                    "-in",
                    str(live / "cert.pem"),
                    "-certfile",
                    str(live / "chain.pem"),
                    "-passout",
                    "pass:",
                ],
                check=True,
                timeout=30,
            )
            imported = certificates.import_certificate(
                args.certificate_name,
                certificate_bytes=pfx.read_bytes(),
                enabled=True,
                tags={
                    "project": "radplanes",
                    "SecurityControl": "Ignore",
                    "acmeEnvironment": "production",
                    "hostname": args.domain,
                },
            )
            if not imported.secret_id:
                raise RuntimeError("Key Vault import did not return a certificate secret")
            return f"{vault_url}/secrets/{args.certificate_name}"
    finally:
        certificates.close()
        secrets.close()
        credential.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--vault-name", required=True)
    parser.add_argument("--certificate-name", required=True)
    parser.add_argument("--account-secret", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--staging", action="store_true")
    args = parser.parse_args()
    uri = issue_in_cluster(args)
    result = json.dumps(
        {"stagingValidation": "passed"} if args.staging else {"certificateSecretUri": uri}
    )
    Path("/dev/termination-log").write_text(result)
    print(result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Certificate issuance failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
