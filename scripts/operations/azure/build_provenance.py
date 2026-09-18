"""Bind canonical source builds to authenticated ACR runs and ARM-owned registry tags.

ARM tag writers, the selected checkout/operator, ACR Tasks, and its trusted verifier are
trusted. An ACR data-plane publisher alone cannot create this provenance. Pending
receipts support retries; unrecorded runs require explicit source/log verification
and full image inspection before a completed v2 proof is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import stat
import sys
from datetime import datetime
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
    require(isinstance(value, dict), "invalid_acr_run")
    result = value.get("properties", value)
    require(isinstance(result, dict), "invalid_acr_run")
    return result


def run_digest(
    run: dict, host: str, component: str, revision: str, run_id: str, staging: str = ""
) -> str:
    data = run_properties(run)
    platform = data.get("platform")
    require(
        isinstance(run_id, str)
        and data.get("runId") == run_id
        and bool(re.fullmatch(r"[a-zA-Z0-9]{1,32}", run_id))
        and data.get("status") == "Succeeded"
        and data.get("runType") in {"QuickBuild", "QuickRun"}
        and isinstance(platform, dict)
        and str(platform.get("os", "")).lower() == "linux"
        and str(platform.get("architecture", "")).lower() == "amd64",
        "untrusted_acr_build_run",
    )
    images = data.get("outputImages")
    require(
        isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict),
        "unexpected_acr_run_outputs",
    )
    image = images[0]
    require(
        image.get("registry") == host
        and image.get("repository") == f"plane-{component}"
        and isinstance(image.get("tag"), str)
        and bool(re.fullmatch(f"build-{revision}-[a-f0-9]{{32}}", image["tag"]))
        and (not staging or staging == f"plane-{component}:{image.get('tag')}")
        and isinstance(image.get("digest"), str)
        and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", image["digest"])),
        "unexpected_acr_run_image",
    )
    return image["digest"]


def intent_record(component, revision, fingerprint, nonce, run_id="", *, recovery=False):
    key = proof_key(component, revision)
    require(bool(re.fullmatch(r"[a-f0-9]{32}", nonce)), "invalid_build_nonce")
    require(
        isinstance(run_id, str) and (not run_id or re.fullmatch(r"[a-zA-Z0-9]{1,32}", run_id)),
        "invalid_build_run_id",
    )
    prefix = "recovery-v1" if recovery else "pending-v1"
    return {
        "state": "pending",
        "key": key,
        "value": f"{prefix}:{nonce}:{fingerprint}:{run_id or '-'}",
        "staging": f"plane-{component}:build-{revision}-{nonce}",
        "runId": run_id,
        "origin": prefix,
        "nonce": nonce,
    }


def build_state(registry, component, revision, fingerprint):
    key = proof_key(component, revision)
    require(isinstance(registry, dict), "invalid_registry_proof")
    tags = registry.get("tags", {})
    require(isinstance(tags, dict), "invalid_registry_tags")
    value = tags.get(key)
    if value is None:
        return {"state": "missing", "key": key, "value": None}
    require(isinstance(value, str), "invalid_arm_build_proof")
    parts = value.split(":")
    if parts[0] == "v2":
        require(len(parts) == 5, "invalid_arm_build_proof")
        checked = record_parts(registry, component, revision, fingerprint, "sha256:" + parts[2])
        return {"state": "verified", "key": key, "value": value, "runId": checked[1]}
    require(
        len(parts) == 4 and parts[0] in {"pending-v1", "recovery-v1"} and parts[2] == fingerprint,
        "pending_build_context_missing_or_mismatched",
    )
    result = intent_record(
        component,
        revision,
        fingerprint,
        parts[1],
        "" if parts[3] == "-" else parts[3],
        recovery=parts[0] == "recovery-v1",
    )
    require(result["value"] == value, "invalid_pending_build_record")
    return result


def dockerfile_instructions(path):
    instructions, pending = [], ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
        else:
            instructions.append(pending + line)
            pending = ""
    require(not pending and bool(instructions), "unsupported_recovery_dockerfile")
    return instructions


def instruction_tokens(value):
    tokens = shlex.shlex(value, posix=False)
    tokens.whitespace_split = True
    tokens.commenters = ""
    return list(tokens)


def recovery_record(root, run, logs, host, component, revision, fingerprint, api_base, commit_time):
    require(component == "api" and not api_base, "explicit_recovery_requires_api_build")
    data = run_properties(run)
    run_id = data.get("runId")
    run_digest(run, host, component, revision, run_id)
    require(data.get("logArtifact") is None, "recovery_requires_acr_managed_logs")
    created = datetime.fromisoformat(data.get("createTime", ""))
    require(
        created.tzinfo is not None
        and commit_time is not None
        and created.timestamp() >= commit_time,
        "recovery_run_predates_source",
    )
    expected = dockerfile_instructions(root / f"images/{component}/Dockerfile")
    steps = [
        match.groups()
        for line in logs.splitlines()
        if (match := re.fullmatch(r"Step (\d+)/(\d+) : (.+)", line))
    ]
    require(len(steps) == len(expected), "recovery_build_instructions_mismatch")
    arguments = {"API_IMAGE": api_base, "SOURCE_REVISION": revision, "TARGETARCH": "amd64"}
    for index, ((number, total, actual), instruction) in enumerate(
        zip(steps, expected, strict=True), 1
    ):
        require(int(number) == index and int(total) == len(expected), "invalid_recovery_build_log")
        expanded = re.sub(
            r"\$\{(API_IMAGE|SOURCE_REVISION|TARGETARCH)\}|\$(API_IMAGE|SOURCE_REVISION|TARGETARCH)\b",
            lambda match: arguments[match[1] or match[2]],
            instruction,
        )
        require(
            instruction_tokens(actual)
            in (instruction_tokens(instruction), instruction_tokens(expanded)),
            "recovery_build_instructions_mismatch",
        )
    nonce = data["outputImages"][0]["tag"].removeprefix(f"build-{revision}-")
    return intent_record(component, revision, fingerprint, nonce, run_id, recovery=True)


def resolve_run(runs: list, host: str, component: str, revision: str, staging: str) -> dict:
    proof_key(component, revision)
    require(
        bool(re.fullmatch(f"plane-{component}:build-{revision}-[a-f0-9]{{32}}", staging)),
        "invalid_fresh_build_staging",
    )
    require(isinstance(runs, list), "invalid_acr_run_list")
    repository, tag = staging.split(":", 1)
    matches = []
    for run in runs:
        require(isinstance(run, dict), "invalid_acr_run")
        images = run_properties(run).get("outputImages")
        if images is None:
            continue
        require(
            isinstance(images, list) and all(isinstance(image, dict) for image in images),
            "invalid_acr_run_outputs",
        )
        if any(
            image.get("registry") == host
            and image.get("repository") == repository
            and image.get("tag") == tag
            for image in images
        ):
            matches.append(run)
    require(len(matches) == 1, "fresh_acr_run_missing_or_ambiguous")
    run = matches[0]
    run_id = run_properties(run).get("runId")
    run_digest(run, host, component, revision, run_id, staging)
    return run


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
    """Bind the invocation's matched ARM run receipt to its current authenticated run."""
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
    parser.add_argument(
        "action",
        choices=["context", "state", "intent", "recover", "lookup", "resolve", "fresh", "verify"],
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--api-base", default="")
    parser.add_argument("--registry-info", type=Path)
    parser.add_argument("--run-info", type=Path)
    parser.add_argument("--runs-info", type=Path)
    parser.add_argument("--run-logs", type=Path)
    parser.add_argument("--commit-time", type=int)
    parser.add_argument("--nonce", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--queued-info", type=Path)
    parser.add_argument("--host", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument("--staging", default="")
    args = parser.parse_args()
    try:
        fingerprint = source_fingerprint(args.source, args.component, args.revision, args.api_base)
        if args.action == "context":
            print(fingerprint)
        elif args.action == "state":
            require(args.registry_info is not None, "missing_registry_proof")
            print(
                json.dumps(
                    build_state(
                        json.loads(args.registry_info.read_text()),
                        args.component,
                        args.revision,
                        fingerprint,
                    )
                )
            )
        elif args.action == "intent":
            print(
                json.dumps(
                    intent_record(
                        args.component, args.revision, fingerprint, args.nonce, args.run_id
                    )
                )
            )
        elif args.action == "recover":
            require(args.run_info is not None and args.run_logs is not None, "missing_recovery_run")
            print(
                json.dumps(
                    recovery_record(
                        args.source,
                        json.loads(args.run_info.read_text()),
                        args.run_logs.read_text(),
                        args.host,
                        args.component,
                        args.revision,
                        fingerprint,
                        args.api_base,
                        args.commit_time,
                    )
                )
            )
        elif args.action == "resolve":
            require(args.runs_info is not None, "missing_acr_run_list")
            print(
                json.dumps(
                    resolve_run(
                        json.loads(args.runs_info.read_text()),
                        args.host,
                        args.component,
                        args.revision,
                        args.staging,
                    )
                )
            )
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
