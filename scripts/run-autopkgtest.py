#!/usr/bin/env python3
"""Execute an isolated installed-package gate for one validated source build."""
import argparse
import json
from pathlib import Path
from packagetest.autopkgtest import exit_status, run

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest', type=Path, required=True)
parser.add_argument('--source', required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--backend', choices=['qemu', 'lxd'], default='qemu')
parser.add_argument('--image', required=True)
parser.add_argument('--dependency-repository', type=Path)
parser.add_argument('--timeout', type=int, default=7200)
args = parser.parse_args()
report = run(args.manifest, args.source, args.output.resolve(), backend=args.backend,
             image=args.image, dependency_repository=args.dependency_repository, timeout=args.timeout)
print(json.dumps(report, indent=2))
# Skips and absent tests must be visible to the generation gate, not silently green.
raise SystemExit(exit_status(report['result']))
