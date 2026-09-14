#!/usr/bin/env python3
"""Optionally inspect and validate bootstrap inputs for troubleshooting."""

import hashlib
import json
import sys
from datetime import UTC, datetime

from project import (
    LOCATION,
    PROJECT,
    SUBSCRIPTION,
    CommandError,
    bootstrap,
    run,
    state_dir,
    write_json,
)


def main() -> int:
    state = state_dir("azure")
    previous = state / "validation.json"
    if previous.exists():
        previous.unlink()
    bootstrap(preview=True)
    template = state / "bootstrap.json"
    parameters = state / "bootstrap.parameters.json"
    output = run(
        [
            "az",
            "deployment",
            "sub",
            "validate",
            "--subscription",
            SUBSCRIPTION,
            "--location",
            LOCATION,
            "--name",
            f"{PROJECT}-bootstrap",
            "--template-file",
            str(template),
            "--parameters",
            f"@{parameters}",
            "--output",
            "json",
        ],
        capture=True,
    )
    result = json.loads(output)
    if result.get("error"):
        raise CommandError("Azure template validation returned an error")
    write_json(
        previous,
        {
            "status": "passed",
            "verified_at": datetime.now(UTC).isoformat(),
            "subscription": SUBSCRIPTION,
            "location": LOCATION,
            "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
            "parameters_sha256": hashlib.sha256(parameters.read_bytes()).hexdigest(),
        },
    )
    print("Bootstrap what-if and Azure validation passed; exact input hashes recorded.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
