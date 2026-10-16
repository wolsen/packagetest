"""Construct the final build DAG from immutable prepared source packages."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from packagetest.config import default_config_path
import hashlib
import json
import os
from pathlib import Path
import sys

from packagetest.prepared_plan import (apply_prepared_metadata, exact_dependencies,
                                       load_prepared, DependencyPlan)


def _edge_summary_rows(edge: dict) -> Iterator[str]:
    """Render every recorded reason for a candidate edge, escaping table separators."""
    reasons = edge.get('reasons') or [{}]
    for reason in reasons:
        relation = str(reason.get('relation', '')).replace('|', '\\|')
        yield (f"| `{edge['source']}` | `{edge['dependency']}` | "
                     f"{reason.get('phase', '')} | `{reason.get('binary', '')}` | "
                     f"{relation} |")


def exact_summary(plan: dict) -> str:
    """Summarize exact candidate edges and archive bootstrap decisions."""
    lines = ['', '## Exact prepared-source dependency decisions', '',
             f"{len(plan.get('dependency_edges', []))} same-run candidate edges and "
             f"{len(plan.get('external_dependency_decisions', []))} external dependency decisions.", '']
    if plan.get('dependency_edges'):
        lines.extend(['| Consumer | Producer | Phase | Binary | Declaration |',
                      '|---|---|---|---|---|'])
        for edge in plan['dependency_edges']:
            lines.extend(_edge_summary_rows(edge))
    else:
        lines.append('No prepared source requires another candidate from this run.')

    bootstraps = plan.get('archive_bootstrap_edges', [])
    if bootstraps:
        lines.extend(['', '### Archive bootstrap decisions', '',
                      '| Consumer | Candidate source | Reason |', '|---|---|---|'])
        lines.extend(f"| `{edge['source']}` | `{edge['dependency']}` | {edge['reason']} |"
                     for edge in bootstraps)

    return '\n'.join(lines) + '\n'


def _prepared_graph(catalog: dict, manifests: dict) -> tuple[dict, DependencyPlan]:
    """Require satisfiable exact dependencies before attaching prepared metadata."""
    graph = exact_dependencies(catalog, manifests)
    dependencies = graph.dependencies
    errors = graph.errors
    if errors:
        raise ValueError('Unsatisfied prepared candidate dependencies:\n' +
                         '\n'.join(f"{item['source']}: {item['relation']}: {item['error']}"
                                   for item in errors))

    catalog = apply_prepared_metadata(catalog, manifests)
    for entry in catalog['packages']:
        entry['build_dependencies'] = dependencies[entry['source']]

    return catalog, graph


def _candidate_constraints(catalog: dict, path: Path) -> list[dict]:
    """Select applicable mandatory edges after checking producer availability."""
    all_constraints = json.loads(path.read_text())
    available = {entry['source'] for entry in catalog['packages']}
    _validate_candidate_presence(all_constraints, available)
    return [item for item in all_constraints if item['source'] in available]


def _validate_candidate_presence(constraints: list[dict], available: set[str]) -> None:
    """Reject mandatory candidates omitted from the prepared source selection."""
    missing = _missing_candidates(constraints, available)
    if missing:
        raise ValueError('Prepared selection omitted mandatory candidates: ' +
                         ', '.join(f"{item['source']} -> {item['dependency']}" for item in missing))


def _missing_candidates(constraints: list[dict], available: set[str]) -> list[dict]:
    """Find absent producers required by consumers included in this plan."""
    return [item for item in constraints
            if item['source'] in available and item['dependency'] not in available]


def _dependency_edges(plan: dict, catalog: dict, reasons: dict) -> list[dict]:
    """Preserve scheduled source order while attaching exact declaration evidence."""
    return [
        {'source': source, 'dependency': dependency,
         'reasons': reasons.get((source, dependency), [])}
        for source in plan['sources']
        for dependency in next(entry for entry in catalog['packages']
                               if entry['source'] == source).get('run_dependencies', [])
    ]


def _annotate_plan(plan: dict, catalog: dict, manifests: dict, external: list[dict], reasons: dict) -> None:
    """Attach dependency evidence and prepared artifact identities to the final plan."""
    plan['requested_sources'] = catalog.get('requested_sources', plan['sources'])
    plan['resolution_failures'] = []
    plan['packaging_source_fallbacks'] = [
        {'source': entry['source'], 'error': entry.get('packaging_resolution_error', '')}
        for entry in catalog['packages'] if entry.get('packaging_source_kind') == 'archive']
    plan['external_dependency_decisions'] = external
    plan['dependency_edges'] = _dependency_edges(plan, catalog, reasons)
    plan['prepared_sources'] = {
        source: {'version': manifest['version'], 'binaries': manifest['binaries'],
                 'dsc_sha256': manifest['dsc_sha256']}
        for source, manifest in sorted(manifests.items())
    }


def main() -> int:
    """Validate prepared inputs, schedule builds, and publish the frozen final plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--prepared-inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('nightly-plan'))
    parser.add_argument('--max-waves', type=int, default=256)
    parser.add_argument('--candidate-dependencies', type=Path,
                        default=default_config_path('hibiscus-candidate-dependencies.json'))
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())
    manifests = load_prepared(args.prepared_inputs, catalog)
    catalog, graph = _prepared_graph(catalog, manifests)
    external, reasons = graph.external_decisions, graph.reasons
    from packagetest import nightly_plan as planner
    constraints = _candidate_constraints(catalog, args.candidate_dependencies)
    catalog, plan = planner.plan_catalog(catalog, max_waves=args.max_waves,
                                         candidate_dependencies=constraints)
    _annotate_plan(plan, catalog, manifests, external, reasons)
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
