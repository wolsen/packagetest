#!/usr/bin/env python3
"""Freeze upstream refs once and plan independent snapshot build waves."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def components(graph):
    """Tarjan strongly connected components, deterministic for audit output."""
    index, stack, active, indices, low, result = 0, [], set(), {}, {}, []
    def visit(node):
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for dep in sorted(graph[node]):
            if dep not in indices:
                visit(dep)
                low[node] = min(low[node], low[dep])
            elif dep in active:
                low[node] = min(low[node], indices[dep])
        if low[node] == indices[node]:
            group = []
            while True:
                item = stack.pop()
                active.remove(item)
                group.append(item)
                if item == node:
                    break
            result.append(sorted(group))
    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return result


def plan_catalog(catalog, sources=None, max_waves=12):
    catalog = json.loads(json.dumps(catalog))
    entries = {p['source']: p for p in catalog['packages']}
    if len(entries) != len(catalog['packages']):
        raise ValueError('Duplicate catalog sources')
    selected = set(sources) if sources else set(entries)
    if selected - entries.keys():
        raise ValueError(f'Unknown requested sources: {sorted(selected - entries.keys())}')
    graph = {s: set(entries[s]['build_dependencies']) & selected for s in selected}
    groups = components(graph)
    bootstrap = []
    for source in sorted(graph):
        outside = set(entries[source]['build_dependencies']) - selected
        for dependency in sorted(outside):
            bootstrap.append({'source': source, 'dependency': dependency, 'reason': 'outside explicitly selected pilot'})
    for group in groups:
        if len(group) > 1 or group[0] in graph[group[0]]:
            members = set(group)
            for source in group:
                for dep in sorted(graph[source] & members):
                    graph[source].remove(dep)
                    bootstrap.append({'source': source, 'dependency': dep, 'reason': 'dependency cycle bootstrap', 'component': group})
    completed, waves = set(), []
    while len(completed) < len(graph):
        wave = sorted(s for s, deps in graph.items() if s not in completed and deps <= completed)
        if not wave:
            raise ValueError('Unresolved dependency cycle')
        waves.append(wave)
        completed.update(wave)
    if len(waves) > max_waves:
        raise ValueError(f'{len(waves)} dependency waves exceed workflow capacity {max_waves}')
    for index, wave in enumerate(waves):
        for source in wave:
            entries[source]['run_dependencies'] = sorted(graph[source])
            entries[source]['archive_bootstrap_dependencies'] = sorted(e['dependency'] for e in bootstrap if e['source'] == source)
            entries[source]['wave'] = index
    catalog['packages'] = [entries[s] for s in sorted(selected)]
    return catalog, {'sources': sorted(selected), 'waves': waves, 'archive_bootstrap_edges': bootstrap,
                     'cycle_components': [g for g in groups if len(g) > 1]}


def freeze(entry):
    ref = entry['upstream_ref']
    repository = entry['upstream_repository']
    try:
        if re.fullmatch(r'[0-9a-f]{40}', ref):
            # Reviewed immutable source selection (e.g. a retired dependency).
            # prepare_source verifies the object exists in its full clone.
            entry['upstream_sha'] = ref
            entry.pop('upstream_resolution_error', None)
            return entry
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', ref) or '..' in ref:
            raise ValueError(f'Invalid branch: {ref}')
        result = subprocess.run(['git', 'ls-remote', '--exit-code', repository, f'refs/heads/{ref}'],
                                text=True, capture_output=True, timeout=90, check=True)
        lines = result.stdout.strip().splitlines()
        if len(lines) != 1 or not re.fullmatch(r'[0-9a-f]{40}', lines[0].split()[0]):
            raise ValueError('Upstream reference did not resolve to exactly one commit')
        entry['upstream_sha'] = lines[0].split()[0]
        entry.pop('upstream_resolution_error', None)
    except Exception as exc:
        entry['upstream_resolution_error'] = str(exc)
        entry['upstream_sha'] = None
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path('config/hibiscus-catalog.json'))
    parser.add_argument('--output', type=Path, default=Path('nightly-plan'))
    parser.add_argument('--sources', default='', help='Comma-separated pilot sources; omitted selects entire catalog')
    parser.add_argument('--no-resolve', action='store_true', help='Offline graph inspection only; not a buildable frozen catalog')
    parser.add_argument('--max-waves', type=int, default=12)
    parser.add_argument('--dependency-pattern', help='Emit artifact download pattern for one frozen catalog source')
    parser.add_argument('--include-self', action='store_true')
    parser.add_argument('--summary-inputs', type=Path)
    args = parser.parse_args()
    if args.summary_inputs:
        sources = [p['source'] for p in json.loads(args.catalog.read_text())['packages']]
        rows = []
        for source in sources:
            row = {'source': source}
            for phase in ('build', 'autopkgtest'):
                paths = list((args.summary_inputs / f'status-{phase}-{source}').rglob('result.json'))
                row[phase] = json.loads(paths[0].read_text()) if len(paths) == 1 else {'result': 'MISSING', 'error': 'Job produced no unique status report'}
            rows.append(row)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
        table = '| Source | Build | Autopkgtest |\n|---|---|---|\n' + ''.join(f"| {row['source']} | {row['build']['result']} | {row['autopkgtest']['result']} |\n" for row in rows)
        (args.output / 'summary.md').write_text(table)
        if os.getenv('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
                handle.write(table)
        print(table)
        return
    if args.dependency_pattern:
        entries = {p['source']: p for p in json.loads(args.catalog.read_text())['packages']}
        needed = set()
        def add(source):
            for dep in entries[source].get('run_dependencies', []):
                if dep not in needed:
                    needed.add(dep)
                    add(dep)
        add(args.dependency_pattern)
        if args.include_self:
            needed.add(args.dependency_pattern)
        names = ['build-' + source for source in sorted(needed)]
        pattern = names[0] if len(names) == 1 else '{' + ','.join(names) + '}' if names else '__no_dependencies__'
        if os.getenv('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
                handle.write('pattern=' + pattern + '\n')
                handle.write('needed=' + ('true' if names else 'false') + '\n')
        print(pattern)
        return
    catalog, plan = plan_catalog(json.loads(args.catalog.read_text()), [s.strip() for s in args.sources.split(',') if s.strip()] or None, args.max_waves)
    if not args.no_resolve:
        with ThreadPoolExecutor(max_workers=16) as workers:
            catalog['packages'] = list(workers.map(freeze, catalog['packages']))
    catalog['resolved_at'] = datetime.now(timezone.utc).isoformat()
    catalog['ci'] = {'run_id': os.getenv('GITHUB_RUN_ID', 'local'), 'run_attempt': os.getenv('GITHUB_RUN_ATTEMPT', '1')}
    args.output.mkdir(parents=True, exist_ok=True)
    content = json.dumps(catalog, indent=2) + '\n'
    (args.output / 'catalog.json').write_text(content)
    plan['catalog_sha256'] = hashlib.sha256(content.encode()).hexdigest()
    plan['resolution_failures'] = [{'source': p['source'], 'error': p['upstream_resolution_error']} for p in catalog['packages'] if p.get('upstream_resolution_error')]
    (args.output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
            handle.write('sources=' + json.dumps({'source': plan['sources']}, separators=(',', ':')) + '\n')
            for index in range(args.max_waves):
                wave = plan['waves'][index] if index < len(plan['waves']) else []
                handle.write(f'wave{index}=' + json.dumps({'source': wave or ['__empty__']}, separators=(',', ':')) + '\n')
                handle.write(f'enabled{index}=' + ('true' if wave else 'false') + '\n')
    print(json.dumps({'packages': len(plan['sources']), 'waves': [len(w) for w in plan['waves']],
                      'archive_bootstrap_edges': len(plan['archive_bootstrap_edges']), 'resolution_failures': len(plan['resolution_failures'])}))

if __name__ == '__main__':
    main()
