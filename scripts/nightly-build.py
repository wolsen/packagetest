#!/usr/bin/env python3
"""Compatibility entry point for :mod:`packagetest.nightly_build`."""

import sys
from pathlib import Path

# Support a repository checkout before installation, as the CI wrappers do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from packagetest.nightly_build import main

if __name__ == "__main__":
    sys.exit(main())
