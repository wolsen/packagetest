#!/usr/bin/env python3
"""Attach build and autopkgtest validation to a packaging proposal."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ACCEPTED_TEST_RESULTS = {'PASS', 'SUPERFICIAL', 'NO_TESTS'}


def finalize(proposal_path: Path, build_path: Path, test_path: Path) -> dict | None:
    if not proposal_path.exists():
        return None
    proposal = json.loads(proposal_path.read_text())
    build = json.loads(build_path.read_text())
    test = json.loads(test_path.read_text())
    proposal['validation'] = {
        'build': {'result': build.get('result'), 'stage': build.get('stage'), 'error': build.get('error')},
        'autopkgtest': {'result': test.get('result'), 'returncode': test.get('returncode'),
                        'error': test.get('error')},
        'ci': test.get('ci') or build.get('ci'),
    }
    if build.get('result') == 'SUCCEEDED' and test.get('result') in ACCEPTED_TEST_RESULTS:
        proposal['status'] = 'validated'
    proposal_path.write_text(json.dumps(proposal, indent=2) + '\n')
    return proposal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('proposal', type=Path)
    parser.add_argument('build', type=Path)
    parser.add_argument('autopkgtest', type=Path)
    args = parser.parse_args()
    finalize(args.proposal, args.build, args.autopkgtest)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
