#!/usr/bin/env python3
"""Build one frozen catalog snapshot with verified same-run dependencies."""
import argparse
import json
from pathlib import Path
import sys
import traceback

from packagetest.artifacts import fields, sha256
from packagetest.catalog import dependency_names
from packagetest.handoff import build_repository, collect_producers, stamp_generation
from packagetest.locked import LockedBuild
from packagetest.nightly_source import prepare_source

TARGET = {'suite': 'resolute', 'architecture': 'amd64', 'backend': 'schroot',
          'chroot': 'resolute-amd64-sbuild', 'openstack_series': '2026.2'}


def dependencies(entries, source):
    result = set()
    def visit(name):
        for dep in entries[name].get('run_dependencies', []):
            if dep not in result:
                result.add(dep)
                visit(dep)
    visit(source)
    return result


def required_versions(source_fields, producers):
    """Select exact producer versions from the adapted source's dependencies."""
    names = dependency_names(', '.join(source_fields.get(key, '') for key in
                             ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch')))
    return {binary['package']: binary['version'] for binaries in producers.values()
            for binary in binaries if binary['package'] in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--check-dependencies-only', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    state = {'source': args.source, 'result': 'PREPARING', 'stage': 'dependency-handoff'}
    try:
        catalog = json.loads(args.catalog.read_text())
        identity = {'run_id': args.run_id, 'run_attempt': args.run_attempt}
        if catalog.get('ci') != identity:
            raise ValueError('Frozen catalog belongs to another pipeline run or attempt')
        entries = {p['source']: p for p in catalog['packages']}
        entry = entries[args.source]
        required = dependencies(entries, args.source)
        locks = {}
        for path in args.inputs.rglob('build-lock.json'):
            lock = json.loads(path.read_text())
            name = lock.get('catalog_entry', {}).get('source')
            if name in required:
                if name in locks:
                    raise ValueError(f'Duplicate dependency lock: {name}')
                if lock['catalog_entry'] != entries[name]:
                    raise ValueError(f'Dependency input differs from frozen catalog: {name}')
                locks[name] = lock
        if required - locks.keys():
            state['result'] = 'BLOCKED'
            raise ValueError(f'Missing dependency builds: {sorted(required - locks.keys())}')
        producers = collect_producers(args.inputs, locks, run_id=args.run_id,
                                      run_attempt=args.run_attempt, target=TARGET)
        if args.check_dependencies_only:
            state['result'] = 'READY'
            return 0
        if producers:
            build_repository(producers, args.output / 'dependency-repository')
        state['stage'] = 'snapshot-source'
        prepared = prepare_source(entry, args.output / 'source-preparation')
        dsc = Path(prepared['dsc'])
        source_fields = fields(dsc)
        version = source_fields['Version']
        # Reviewed packaging adaptations can add or tighten dependencies. Pin
        # the dependencies of the source we actually build, not its baseline.
        versions = required_versions(source_fields, producers)
        package = {'source': args.source, 'version': version, 'expected_binaries': entry['binaries'],
                   'external_dependencies': sorted(producers), 'required_build_versions': versions,
                   'input': {'kind': 'prepared-snapshot', 'dsc_sha256': sha256(dsc),
                             'upstream_sha': entry['upstream_sha']}}
        lock = {'schema_version': 1, 'target': TARGET, 'catalog_entry': entry,
                'maintainer': {'name': 'Packaging Build Agent', 'email': 'packaging-agent@example.invalid'},
                'packages': [package]}
        state['stage'] = 'binary-build'
        build = LockedBuild(lock, args.output, prepared_sources={args.source: dsc},
                            external_artifacts=producers, timeout=5400)
        result = build.run()
        stamp_generation(build.root / 'generation-manifest.json', run_id=args.run_id, run_attempt=args.run_attempt)
        state.update(result=build.manifest['result'], generation=str(build.root))
        return result
    except Exception as error:
        state.update(result='BLOCKED' if state['stage'] == 'dependency-handoff' else 'FAILED', error=str(error))
        traceback.print_exc()
        return 1
    finally:
        if state['result'] != 'READY':
            (args.output / 'result.json').write_text(json.dumps(state, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
