#!/usr/bin/env python3
"""Emit the source-only local Recipe bundle; never contact Docker, Kubernetes, or Azure."""

import argparse
import json
import sys

from common import LocalError
from prepare import recipe_bundle


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        print(json.dumps(recipe_bundle()))
    except (LocalError, OSError, ValueError) as error:
        print(f"Recipe bundle failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
