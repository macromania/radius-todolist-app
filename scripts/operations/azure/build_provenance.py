"""Bind canonical source builds to authenticated ACR runs and ARM-owned registry tags.

ARM tag writers, the selected checkout/operator, ACR Tasks, and Docker Desktop are
trusted. An ACR data-plane publisher alone cannot create this provenance. Existing
images without a v2 record are refused, never retrospectively attested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path

POLICY = "acr-canonical-v2"


class ProvenanceError(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProvenanceError(code)


def proof_key(component: str, revision: str) -> str:
    require(component in {"api", "provisioner"}, "invalid_provenance_component")
    require(bool(re.fullmatch(r"[a-f0-9]{40}", revision)), "invalid_provenance_revision")
    return f"plane-demo-proof-{component}-{revision}"


def source_fingerprint(root: Path, component: str, revision: str, api_base: str = "") -> str:
    proof_key(component, revision)
    require(
        not api_base
        if component == "api"
        else bool(re.fullmatch(r"[a-z0-9]+\.azurecr\.io/plane-api@sha256:[a-f0-9]{64}", api_base)),
        "invalid_provenance_api_base",
    )
    sources = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "provenance_source_symlink")
        if path.is_dir():
            continue
        # Radius-generated bundles are checked by member content during export
        # inspection; their archive timestamps are not committed build inputs.
        if path.suffix == ".tgz" and path.is_relative_to(root / "infra/radius/types"):
            continue
        require(path.is_file(), "invalid_provenance_source")
        sources[path.relative_to(root).as_posix()] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
    require(bool(sources), "empty_provenance_source")
    definition = {
        "policy": POLICY,
        "revision": revision,
        "component": component,
        "platform": "linux/amd64",
        "source_acr_auth_id": "[caller]",
        "dockerfile": f"images/{component}/Dockerfile",
        "build_args": {
            "SOURCE_REVISION": revision,
            **(
                {"TARGETARCH": "amd64", "API_IMAGE": api_base} if component == "provisioner" else {}
            ),
        },
        "radius": "0.60.2",
        "bicep": "0.42.1",
        "sources": sources,
    }
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_properties(value: dict) -> dict:
    result = value.get("properties", value)
    require(isinstance(result, dict), "invalid_acr_run")
    return result


def run_digest(
    run: dict, host: str, component: str, revision: str, run_id: str, staging: str = ""
) -> str:
    data = run_properties(run)
    require(
        data.get("runId") == run_id
        and bool(re.fullmatch(r"[a-zA-Z0-9]{1,32}", run_id))
        and data.get("status") == "Succeeded"
        and data.get("runType") == "QuickBuild"
        and str(data.get("platform", {}).get("os", "")).lower() == "linux"
        and str(data.get("platform", {}).get("architecture", "")).lower() == "amd64",
        "untrusted_acr_build_run",
    )
    images = data.get("outputImages")
    require(isinstance(images, list) and len(images) == 1, "unexpected_acr_run_outputs")
    image = images[0]
    require(
        image.get("registry") == host
        and image.get("repository") == f"plane-{component}"
        and bool(re.fullmatch(f"build-{revision}-[a-f0-9]{{32}}", image.get("tag", "")))
        and (not staging or staging == f"plane-{component}:{image.get('tag')}")
        and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("digest", ""))),
        "unexpected_acr_run_image",
    )
    return image["digest"]


def record_parts(
    registry: dict, component: str, revision: str, fingerprint: str, digest: str
) -> list[str]:
    value = registry.get("tags", {}).get(proof_key(component, revision), "")
    require(isinstance(value, str), "invalid_arm_build_proof")
    parts = value.split(":")
    require(
        len(parts) == 5
        and parts[0] == "v2"
        and bool(re.fullmatch(r"[a-zA-Z0-9]{1,32}", parts[1]))
        and bool(re.fullmatch(r"[a-f0-9]{64}", parts[2]))
        and parts[3] == fingerprint
        and digest == "sha256:" + parts[2]
        and bool(re.fullmatch(r"[a-f0-9]{64}", parts[4])),
        "arm_build_provenance_missing_or_mismatched",
    )
    return parts


def evidence(run_id: str, digest: str, fingerprint: str) -> dict:
    return {"policy": POLICY, "run_id": run_id, "digest": digest, "source_fingerprint": fingerprint}


def fresh_record(
    queued: dict,
    run: dict,
    host: str,
    component: str,
    revision: str,
    fingerprint: str,
    staging: str,
) -> dict:
    run_id = run_properties(queued).get("runId", "")
    require(bool(staging), "fresh_build_staging_required")
    digest = run_digest(run, host, component, revision, run_id, staging)
    return {
        "key": proof_key(component, revision),
        "value": f"v2:{run_id}:{digest.removeprefix('sha256:')}:{fingerprint}",
        "provenance": evidence(run_id, digest, fingerprint),
    }


def verify_record(
    registry: dict,
    run: dict,
    host: str,
    component: str,
    revision: str,
    fingerprint: str,
    digest: str,
) -> dict:
    parts = record_parts(registry, component, revision, fingerprint, digest)
    require(
        run_digest(run, host, component, revision, parts[1]) == digest,
        "arm_proof_acr_run_mismatch",
    )
    return {**evidence(parts[1], digest, fingerprint), "filesystem_sha256": parts[4]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["context", "lookup", "fresh", "verify"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--api-base", default="")
    parser.add_argument("--registry-info", type=Path)
    parser.add_argument("--run-info", type=Path)
    parser.add_argument("--queued-info", type=Path)
    parser.add_argument("--host", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument("--staging", default="")
    args = parser.parse_args()
    try:
        fingerprint = source_fingerprint(args.source, args.component, args.revision, args.api_base)
        if args.action == "context":
            print(fingerprint)
        elif args.action == "fresh":
            require(args.queued_info is not None and args.run_info is not None, "missing_fresh_run")
            print(
                json.dumps(
                    fresh_record(
                        json.loads(args.queued_info.read_text()),
                        json.loads(args.run_info.read_text()),
                        args.host,
                        args.component,
                        args.revision,
                        fingerprint,
                        args.staging,
                    )
                )
            )
        else:
            require(args.registry_info is not None, "missing_registry_proof")
            registry = json.loads(args.registry_info.read_text())
            if args.action == "lookup":
                print(
                    record_parts(registry, args.component, args.revision, fingerprint, args.digest)[
                        1
                    ]
                )
            else:
                require(args.run_info is not None, "missing_acr_run")
                print(
                    json.dumps(
                        verify_record(
                            registry,
                            json.loads(args.run_info.read_text()),
                            args.host,
                            args.component,
                            args.revision,
                            fingerprint,
                            args.digest,
                        )
                    )
                )
    except ProvenanceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print("ERROR: invalid_build_provenance_input", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
