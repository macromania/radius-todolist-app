#!/usr/bin/env python3
"""Compatibility entrypoint; Radius administration is implemented in Bash."""

import os
import sys
from pathlib import Path

if __name__ == "__main__":
    script = Path(__file__).with_suffix(".sh")
    os.execv("/bin/bash", ["bash", str(script), *sys.argv[1:]])
