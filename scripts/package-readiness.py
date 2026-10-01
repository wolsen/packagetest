#!/usr/bin/env python3
"""Bind a producer handoff to its final build and autopkgtest verdict."""
import argparse
import json
from pathlib import Path


ACCEPTED_TESTS = {'PASS', 'SUPERFICIAL', 'SKIP', 'NO_TESTS'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--outputs', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--autopkgtest', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    args = parser.parse_args()

    manifests = list(args.outputs.glob('gen-*/generation-manifest.json'))
    if len(manifests) != 1:
        raise ValueError(f'Expected one generation manifest, found {len(manifests)}')
    build = json.loads(args.build.read_text())
    test = json.loads(args.autopkgtest.read_text())
    build_result = build.get('result')
    test_result = test.get('result')
    ready = build_result == 'SUCCEEDED' and test_result in ACCEPTED_TESTS
    report = {
        'schema_version': 1,
        'source': args.source,
        'result': 'READY' if ready else 'FAILED',
        'build_result': build_result,
        'autopkgtest_result': test_result,
        'ci': {'run_id': str(args.run_id), 'run_attempt': str(args.run_attempt)},
    }
    output = manifests[0].parent / 'producer-readiness.json'
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
