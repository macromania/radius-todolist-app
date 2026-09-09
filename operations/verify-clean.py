#!/usr/bin/env python3
"""Read-only verification of the exact Azure ownership manifest."""

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    spec = importlib.util.spec_from_file_location(
        "azure_cleanup", Path(__file__).with_name("clean-azure.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.main(verify_only=True)


if __name__ == "__main__":
    raise SystemExit(main())
