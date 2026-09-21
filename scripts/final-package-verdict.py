#!/usr/bin/env python3
"""Fail a package job unless its final build and test candidates are usable."""
import argparse
import json
from pathlib import Path

ACCEPTED_TESTS = {"PASS", "SUPERFICIAL", "SKIP", "NO_TESTS"}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("build", type=Path)
parser.add_argument("test", type=Path)
args = parser.parse_args()

build = json.loads(args.build.read_text())
test = json.loads(args.test.read_text())
build_result = build.get("result")
test_result = test.get("result")
print(f"final build={build_result} autopkgtest={test_result}")
if build_result != "SUCCEEDED" or test_result not in ACCEPTED_TESTS:
    raise SystemExit(1)
