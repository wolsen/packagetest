#!/usr/bin/env python3
"""Prepare one immutable snapshot source and export its exact Debian metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import traceback

from packagetest.artifacts import checksum_entries, fields, sha256, verify_source
from packagetest.catalog import paragraphs
from packagetest.nightly_source import prepare_source


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def control_metadata(tree: Path) -> dict:
    records = paragraphs((tree / 'debian/control').read_text())
    if not records or 'Source' not in records[0]:
        raise ValueError('Prepared debian/control has no source paragraph')
    source = records[0]
    binaries = []
    for record in records[1:]:
        if 'Package' not in record:
            continue
        binaries.append({
            'package': record['Package'],
            'architecture': record.get('Architecture', 'any'),
            'depends': record.get('Depends', ''),
            'pre_depends': record.get('Pre-Depends', ''),
            'recommends': record.get('Recommends', ''),
            'build_profiles': record.get('Build-Profiles', ''),
        })
    tests = []
    tests_control = tree / 'debian/tests/control'
    if tests_control.is_file():
        for record in paragraphs(tests_control.read_text()):
            tests.append({
                'tests': record.get('Tests', record.get('Test-Command', 'unnamed')),
                'depends': record.get('Depends', ''),
                'restrictions': record.get('Restrictions', ''),
                'features': record.get('Features', ''),
            })
    return {
        'build_depends': {key: source.get(key, '') for key in
                          ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch')},
        'binary_packages': binaries,
        'autopkgtests': tests,
    }


def export_prepared(prepared: dict, entry: dict, catalog: dict, output: Path) -> dict:
    dsc = Path(prepared['dsc'])
    tree = Path(prepared['tree'])
    source_fields = verify_source(dsc, entry['source'], fields(dsc)['Version'])
    metadata = control_metadata(tree)
    declared = sorted(name.strip() for name in source_fields.get('Binary', '').split(',')
                      if name.strip())
    control_declared = sorted(item['package'] for item in metadata['binary_packages'])
    if declared != control_declared:
        raise ValueError(f'.dsc binaries differ from prepared debian/control: '
                         f'{declared} != {control_declared}')
    if not declared:
        raise ValueError('Prepared source declares no binary packages')

    output.mkdir(parents=True, exist_ok=False)
    source_dir = output / 'source'
    source_dir.mkdir()
    files = [dsc, *(dsc.parent / name for _, _, name in checksum_entries(source_fields))]
    for path in files:
        shutil.copy2(path, source_dir / path.name)
    proposal = dsc.parent / 'packaging-proposal'
    if proposal.is_dir():
        shutil.copytree(proposal, output / 'packaging-proposal')
    resolution = Path(prepared['lock'])
    shutil.copy2(resolution, output / 'resolution.json')

    manifest = {
        'schema_version': 1,
        'source': entry['source'],
        'version': source_fields['Version'],
        'binaries': declared,
        **metadata,
        'testsuite': source_fields.get('Testsuite', ''),
        'testsuite_triggers': source_fields.get('Testsuite-Triggers', ''),
        'dsc': dsc.name,
        'dsc_sha256': sha256(dsc),
        'source_artifacts': [{'file': path.name, 'sha256': sha256(path)} for path in files],
        'catalog_entry_sha256': digest(entry),
        'pins': {'upstream_sha': entry['upstream_sha'],
                 'packaging_sha': entry.get('packaging_sha')},
        'ci': catalog['ci'],
    }
    (output / 'prepared-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--remediation-patch', type=Path)
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())
    identity = {'run_id': str(args.run_id), 'run_attempt': str(args.run_attempt)}
    if catalog.get('ci') != identity:
        raise ValueError('Frozen catalog belongs to another pipeline run or attempt')
    entries = {entry['source']: entry for entry in catalog['packages']}
    if args.source not in entries:
        raise ValueError(f'Unknown frozen source: {args.source}')
    try:
        prepared = prepare_source(entries[args.source], args.work,
                                  remediation_patch=args.remediation_patch)
        manifest = export_prepared(prepared, entries[args.source], catalog, args.output)
        print(json.dumps({'result': 'PREPARED', 'source': args.source,
                          'version': manifest['version'], 'binaries': manifest['binaries']}))
        return 0
    except Exception as exc:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'result.json').write_text(json.dumps({
            'source': args.source, 'result': 'FAILED', 'stage': 'source-preparation',
            'error': str(exc), 'ci': identity}, indent=2) + '\n')
        if args.work.exists():
            evidence = args.output / 'failure-evidence'
            evidence.mkdir(exist_ok=True)
            for name in ('resolution.json',):
                path = args.work / name
                if path.is_file():
                    shutil.copy2(path, evidence / name)
            logs = args.work / 'logs'
            if logs.is_dir():
                shutil.copytree(logs, evidence / 'logs', dirs_exist_ok=True)
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
