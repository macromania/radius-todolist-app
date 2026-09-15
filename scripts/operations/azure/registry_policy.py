"""Reject effective canonical repository writers outside the declared ABAC policy."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

WRITE_ACTIONS = {
    f"microsoft.containerregistry/registries/repositories/{kind}/{action}"
    for kind in ("content", "metadata")
    for action in ("write", "delete")
}


class RegistryPolicyError(ValueError):
    pass


def repository_writes(definition: dict) -> bool:
    for permission in definition.get("permissions", []):
        allowed = [value.lower() for value in permission.get("dataActions", [])]
        denied = [value.lower() for value in permission.get("notDataActions", [])]
        for action in WRITE_ACTIONS:
            if any(fnmatch.fnmatchcase(action, pattern) for pattern in allowed) and not any(
                fnmatch.fnmatchcase(action, pattern) for pattern in denied
            ):
                return True
    return False


def verify_assignments(assignments: list, definitions: list, policy: dict) -> None:
    roles = {item["name"].lower(): item for item in definitions}
    observed = set()
    expected = " ".join(policy["writerCondition"].split())
    for assignment in assignments:
        role = assignment["roleDefinitionId"].rsplit("/", 1)[-1].lower()
        if role not in roles:
            raise RegistryPolicyError("registry_role_definition_missing")
        observed.add(role)
        if repository_writes(roles[role]) and (
            assignment.get("conditionVersion") != policy["conditionVersion"]
            or " ".join((assignment.get("condition") or "").split()) != expected
        ):
            raise RegistryPolicyError("canonical_recipe_writer_not_isolated")
    required = {
        policy["repositoryReaderRoleId"],
        policy["repositoryWriterRoleId"],
        policy["dataImporterRoleId"],
    }
    if not required.issubset(observed):
        raise RegistryPolicyError("registry_abac_role_assignments_incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--definitions", type=Path, required=True)
    args = parser.parse_args()
    try:
        policy = json.loads(Path(__file__).with_name("registry-policy.json").read_text())
        verify_assignments(
            json.loads(args.assignments.read_text()),
            json.loads(args.definitions.read_text()),
            policy,
        )
    except RegistryPolicyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, ValueError):
        print("ERROR: invalid_registry_permission_records", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
