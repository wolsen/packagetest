#!/usr/bin/env python3
"""Construct the final build DAG from immutable prepared source packages."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

from packagetest.prepared_plan import (apply_prepared_metadata, exact_dependencies,
                                       load_prepared)


def planning_module():
    path = Path(__file__).with_name('nightly-plan.py')
    spec = importlib.util.spec_from_file_location('nightly_plan_final', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exact_summary(plan: dict) -> str:
    lines = ['', '## Exact prepared-source dependency decisions', '',
             f"{len(plan.get('dependency_edges', []))} same-run candidate edges and "
             f"{len(plan.get('external_dependency_decisions', []))} external dependency decisions.", '']
    if plan.get('dependency_edges'):
        lines.extend(['| Consumer | Producer | Phase | Binary | Declaration |',
                      '|---|---|---|---|---|'])
        for edge in plan['dependency_edges']:
            reasons = edge.get('reasons') or [{}]
            for reason in reasons:
                relation = str(reason.get('relation', '')).replace('|', '\\|')
                lines.append(f"| `{edge['source']}` | `{edge['dependency']}` | "
                             f"{reason.get('phase', '')} | `{reason.get('binary', '')}` | "
                             f"{relation} |")
    else:
        lines.append('No prepared source requires another candidate from this run.')
    bootstraps = plan.get('archive_bootstrap_edges', [])
    if bootstraps:
        lines.extend(['', '### Archive bootstrap decisions', '',
                      '| Consumer | Candidate source | Reason |', '|---|---|---|'])
        lines.extend(f"| `{edge['source']}` | `{edge['dependency']}` | {edge['reason']} |"
                     for edge in bootstraps)
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--prepared-inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('nightly-plan'))
    parser.add_argument('--max-waves', type=int, default=256)
    parser.add_argument('--candidate-dependencies', type=Path,
                        default=Path(__file__).resolve().parents[1] /
                        'config/hibiscus-candidate-dependencies.json')
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())
    manifests = load_prepared(args.prepared_inputs, catalog)
    dependencies, external, errors, reasons = exact_dependencies(catalog, manifests)
    if errors:
        raise ValueError('Unsatisfied prepared candidate dependencies:\n' +
                         '\n'.join(f"{item['source']}: {item['relation']}: {item['error']}"
                                   for item in errors))
    catalog = apply_prepared_metadata(catalog, manifests)
    for entry in catalog['packages']:
        entry['build_dependencies'] = dependencies[entry['source']]
    planner = planning_module()
    all_constraints = json.loads(args.candidate_dependencies.read_text())
    available = {entry['source'] for entry in catalog['packages']}
    missing_required = [item for item in all_constraints
                        if item['source'] in available and item['dependency'] not in available]
    if missing_required:
        raise ValueError('Prepared selection omitted mandatory candidates: ' +
                         ', '.join(f"{item['source']} -> {item['dependency']}"
                                   for item in missing_required))
    constraints = [item for item in all_constraints if item['source'] in available]
    catalog, plan = planner.plan_catalog(catalog, max_waves=args.max_waves,
                                         candidate_dependencies=constraints)
    plan['requested_sources'] = catalog.get('requested_sources', plan['sources'])
    plan['resolution_failures'] = []
    plan['packaging_source_fallbacks'] = [
        {'source': entry['source'], 'error': entry.get('packaging_resolution_error', '')}
        for entry in catalog['packages'] if entry.get('packaging_source_kind') == 'archive']
    plan['external_dependency_decisions'] = external
    plan['dependency_edges'] = [
        {'source': source, 'dependency': dependency,
         'reasons': reasons.get((source, dependency), [])}
        for source in plan['sources']
        for dependency in next(entry for entry in catalog['packages']
                               if entry['source'] == source).get('run_dependencies', [])
    ]
    plan['prepared_sources'] = {
        source: {'version': manifest['version'], 'binaries': manifest['binaries'],
                 'dsc_sha256': manifest['dsc_sha256']}
        for source, manifest in sorted(manifests.items())
    }
    args.output.mkdir(parents=True, exist_ok=True)
    catalog_text = json.dumps(catalog, indent=2) + '\n'
    (args.output / 'catalog.json').write_text(catalog_text)
    plan['catalog_sha256'] = hashlib.sha256(catalog_text.encode()).hexdigest()
    (args.output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    summary = planner.render_plan_summary(plan, catalog) + exact_summary(plan)
    (args.output / 'summary.md').write_text(summary)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(summary)
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('dependency_levels=' + json.dumps(
                planner.dependency_level_matrix(plan['waves']), separators=(',', ':')) + '\n')
    print(json.dumps({'packages': len(plan['sources']),
                      'dependency_levels': len(plan['waves']),
                      'candidate_edges': len(plan['dependency_edges']),
                      'external_decisions': len(external)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
