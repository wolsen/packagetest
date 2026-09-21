#!/usr/bin/env python3
"""Verify downloaded same-run bundles and execute one package's autopkgtests."""
import argparse
import json
from pathlib import Path

from packagetest.autopkgtest import exit_status, run
from packagetest.handoff import build_repository, collect_producers

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--catalog', type=Path, required=True)
parser.add_argument('--source', required=True)
parser.add_argument('--inputs', type=Path, required=True)
parser.add_argument('--candidate-input', type=Path,
                    help='Selected candidate root; dependencies remain under --inputs')
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--run-id', required=True)
parser.add_argument('--run-attempt', required=True)
parser.add_argument('--image', required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
report = {'schema_version': 1, 'source': args.source, 'result': 'INFRA_ERROR',
          'ci': {'run_id': str(args.run_id), 'run_attempt': str(args.run_attempt)}}
try:
    catalog = json.loads(args.catalog.read_text())
    entries = {entry['source']: entry for entry in catalog['packages']}
    if args.source not in entries:
        raise ValueError('Requested source is absent from frozen catalog')
    locks, paths, source_roots, unavailable = {}, {}, {}, []
    roots = [args.inputs]
    if args.candidate_input:
        roots.insert(0, args.candidate_input)
    manifests = [(root, path) for root in roots for path in root.rglob('generation-manifest.json')]
    for root, path in sorted(manifests, key=lambda item: str(item[1])):
        manifest = json.loads(path.read_text())
        if manifest.get('ci') != report['ci']:
            raise ValueError(f'Wrong-run artifact: {path}')
        if manifest.get('result') != 'SUCCEEDED':
            unavailable.extend(p['source'] for p in manifest.get('packages', []))
            continue
        lock = json.loads((path.parent / 'build-lock.json').read_text())
        for package in manifest['packages']:
            source = package['source']
            if source not in entries or lock.get('catalog_entry') != entries[source]:
                raise ValueError(f'Producer does not match frozen catalog: {source}')
            if source in paths:
                raise ValueError(f'Duplicate producer: {source}')
            locks[source] = lock
            paths[source] = path
            source_roots[source] = root
    report['unavailable_builds'] = unavailable
    if args.source not in locks:
        report.update(result='BLOCKED', error='No successful same-run build for requested source')
    else:
        target = locks[args.source]['target']
        if target.get('suite') != 'resolute' or target.get('openstack_series') != '2026.2':
            raise ValueError('Unexpected nightly target')
        producers = {}
        for root in roots:
            expected = {name: lock for name, lock in locks.items()
                        if source_roots[name] == root}
            if expected:
                producers.update(collect_producers(root, expected, run_id=args.run_id,
                                                   run_attempt=args.run_attempt, target=target))
        repository = args.output / 'repository'
        build_repository(producers, repository)
        report.update(run(paths[args.source], args.source, args.output / 'autopkgtest',
                          backend='qemu', image=args.image, dependency_repository=repository))
except (OSError, ValueError, KeyError) as error:
    report.update(result='INFRA_ERROR', error=str(error))
finally:
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
raise SystemExit(exit_status(report['result']))
