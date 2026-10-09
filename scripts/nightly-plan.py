#!/usr/bin/env python3
"""Freeze upstream refs once and plan independent snapshot build waves."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def resolve_upstream_dependency_closure(catalog, sources=None, candidate_dependencies=(),
                                        freeze_entry=None, inspect_entry=None):
    """Freeze roots and recursively add their upstream Python dependencies."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from packagetest.upstream_dependencies import (distribution_source_index,
                                                   distribution_archive_index,
                                                   inspect_revision,
                                                   map_requirements)
    from packagetest.catalog import dependency_names
    catalog = json.loads(json.dumps(catalog))
    entries = {entry['source']: entry for entry in catalog['packages']}
    requested = set(sources) if sources else set(entries)
    if requested - entries.keys():
        raise ValueError(f'Unknown requested sources: {sorted(requested - entries.keys())}')
    selected = set(requested)
    reasons = {source: {'requested'} for source in requested}
    index = distribution_source_index(catalog['packages'])
    archive_index = distribution_archive_index(catalog.get('archive_python_packages', []))
    freeze_entry = freeze_entry or freeze
    inspect_entry = inspect_entry or inspect_revision
    inspected = set()

    while True:
        for constraint in candidate_dependencies:
            if constraint['source'] in selected and constraint['dependency'] not in selected:
                selected.add(constraint['dependency'])
                reasons.setdefault(constraint['dependency'], set()).add(
                    f'mandatory candidate for {constraint["source"]}')
        frontier = sorted(selected - inspected)
        if not frontier:
            break
        with ThreadPoolExecutor(max_workers=16) as workers:
            frozen = list(workers.map(freeze_entry, (entries[source] for source in frontier)))
        for entry in frozen:
            entries[entry['source']] = entry

        def inspect(source):
            entry = entries[source]
            if entry.get('upstream_resolution_error'):
                return source, None, [], entry['upstream_resolution_error']
            try:
                records, files = inspect_entry(entry)
                return source, records, files, None
            except Exception as exc:
                return source, None, [], str(exc)

        with ThreadPoolExecutor(max_workers=16) as workers:
            results = list(workers.map(inspect, frontier))
        for source, records, files, error in results:
            entry = entries[source]
            inspected.add(source)
            if error:
                entry['upstream_dependency_resolution_error'] = error
                continue
            mapped, dependencies = map_requirements(
                records, index, source, entries, archive_index)
            entry['upstream_dependency_files'] = files
            entry['upstream_dependency_requirements'] = mapped
            entry['upstream_dependencies'] = dependencies
            packaged = dependency_names(entry.get('build_depends', ''))
            entry['planned_archive_dependency_additions'] = [
                {
                    'binary': record['archive_binary'],
                    'source': record['archive_source'],
                    'archive_version': record['archive_version'],
                    'kind': record['kind'],
                    'requirement': record['requirement'],
                    'file': record['file'],
                    'line': record['line'],
                }
                for record in mapped
                if record.get('kind') in {'test', 'build-system'}
                and record.get('archive_binary')
                and record['archive_binary'] not in packaged
                and record.get('archive_satisfies') is True
            ]
            entry['archive_satisfied_upstream_dependencies'] = sorted({
                record['source'] for record in mapped
                if record.get('source') and record.get('archive_satisfies') is True
                and record.get('source') not in dependencies})
            entry.pop('upstream_dependency_resolution_error', None)
            entry['packaging_build_dependencies'] = list(entry.get('build_dependencies', []))
            entry['build_dependencies'] = sorted(set(entry.get('build_dependencies', [])) | set(dependencies))
            for dependency in dependencies:
                reasons.setdefault(dependency, set()).add(f'upstream dependency of {source}')
                selected.add(dependency)

    for source, entry in entries.items():
        entry['selection_reasons'] = sorted(reasons.get(source, []))
    catalog['packages'] = [entries[source] for source in sorted(entries)]
    errors = [{'source': source, 'error': entries[source]['upstream_dependency_resolution_error']}
              for source in sorted(selected) if entries[source].get('upstream_dependency_resolution_error')]
    return catalog, sorted(selected), sorted(requested), errors


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


def plan_catalog(catalog, sources=None, max_waves=12, candidate_dependencies=()):
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
            satisfied = dependency in entries[source].get('archive_satisfied_upstream_dependencies', [])
            bootstrap.append({'source': source, 'dependency': dependency,
                              'reason': ('Ubuntu archive satisfies upstream requirement' if satisfied
                                         else 'outside explicitly selected pilot')})
    for group in groups:
        if len(group) > 1 or group[0] in graph[group[0]]:
            members = set(group)
            for source in group:
                for dep in sorted(graph[source] & members):
                    graph[source].remove(dep)
                    bootstrap.append({'source': source, 'dependency': dep, 'reason': 'dependency cycle bootstrap', 'component': group})
    # Some snapshot APIs are newer than the archive bootstrap packages. Retain
    # reviewed mandatory candidate edges even inside a cyclic component; the
    # reverse (often documentation-only) edge can still use the archive.
    for constraint in candidate_dependencies:
        source, dep = constraint['source'], constraint['dependency']
        if source not in entries or dep not in entries:
            raise ValueError(f'Unknown mandatory candidate dependency: {source} -> {dep}')
        if dep not in entries[source]['build_dependencies']:
            raise ValueError(f'Mandatory candidate is not a declared build dependency: {source} -> {dep}')
        if source not in selected:
            continue
        if dep not in selected:
            raise ValueError(f'{source} requires candidate {dep}; include it in the selected sources')
        graph[source].add(dep)
        bootstrap = [edge for edge in bootstrap if (edge['source'], edge['dependency']) != (source, dep)]
        entries[source].setdefault('required_candidate_dependencies', {})[dep] = constraint['reason']
    completed, waves = set(), []
    while len(completed) < len(graph):
        wave = sorted(s for s, deps in graph.items() if s not in completed and deps <= completed)
        if not wave:
            raise ValueError('Unresolved dependency cycle')
        waves.append(wave)
        completed.update(wave)
    if len(waves) > max_waves:
        raise ValueError(f'{len(waves)} dependency waves exceed workflow capacity {max_waves}')
    # Reuse candidates that are already available without adding serialization.
    # Removing every SCC edge is only a starting point: an earlier-wave member
    # can safely replace an archive bootstrap dependency without creating cycles.
    order = {source: index for index, wave in enumerate(waves) for source in wave}
    remaining_bootstrap = []
    for edge in bootstrap:
        if edge['reason'] == 'dependency cycle bootstrap' and order[edge['dependency']] < order[edge['source']]:
            graph[edge['source']].add(edge['dependency'])
        else:
            remaining_bootstrap.append(edge)
    bootstrap = remaining_bootstrap
    for index, wave in enumerate(waves):
        for source in wave:
            entries[source]['run_dependencies'] = sorted(graph[source])
            entries[source]['archive_bootstrap_dependencies'] = sorted(e['dependency'] for e in bootstrap if e['source'] == source)
            entries[source]['wave'] = index
    catalog['packages'] = [entries[s] for s in sorted(selected)]
    return catalog, {'sources': sorted(selected), 'waves': waves, 'archive_bootstrap_edges': bootstrap,
                     'cycle_components': [g for g in groups if len(g) > 1]}


def render_plan_summary(plan: dict, catalog: dict) -> str:
    """Render the complete dependency-level plan for GitHub and artifacts."""
    entries = {entry['source']: entry for entry in catalog['packages']}
    lines = [
        f"# OpenStack {catalog.get('series', 'unknown')} snapshot build plan",
        '',
        f"{len(plan['sources'])} source packages across {len(plan['waves'])} dependency levels. "
        'Levels run in order; packages within one level are eligible to run in parallel. '
        'Each package job builds and then runs its autopkgtest before the next level starts.',
        '',
        '| Dependency level | Package count | Packages |',
        '|---:|---:|---|',
    ]
    for index, packages in enumerate(plan['waves'], 1):
        package_list = ', '.join(f'`{source}`' for source in packages)
        lines.append(f'| {index} | {len(packages)} | {package_list} |')

    requested = plan.get('requested_sources', plan['sources'])
    added = sorted(set(plan['sources']) - set(requested))
    upstream_edges = sum(len(entries[source].get('upstream_dependencies', []))
                         for source in plan['sources'])
    decisions = Counter(record.get('archive_decision')
                        for source in plan['sources']
                        for record in entries[source].get('upstream_dependency_requirements', [])
                        if record.get('archive_decision'))
    lines.extend([
        '',
        '## Upstream dependency discovery',
        '',
        f'{len(requested)} requested roots expanded to {len(plan["sources"])} source packages. '
        f'{upstream_edges} same-run candidate dependency edges came from pinned `requirements.txt`, '
        '`test-requirements.txt`, or `pyproject.toml` files.',
        '',
        f'Ubuntu archive checks: {decisions["satisfied"]} satisfied requirements, '
        f'{decisions["insufficient"]} insufficient versions, {decisions["unknown"]} unknown versions, '
        f'and {decisions["unmapped"]} Python requirements with no unambiguous Ubuntu provider. '
        'OpenStack providers that are not satisfied become same-run candidate edges; external results remain '
        'explicit packaging evidence.',
        '',
    ])
    if added:
        lines.append('Automatically added sources: ' + ', '.join(f'`{source}`' for source in added) + '.')
    else:
        lines.append('No additional sources were needed.')

    packaging_additions = [
        (source, record)
        for source in plan['sources']
        for record in entries[source].get('planned_archive_dependency_additions', [])
    ]
    lines.extend([
        '',
        '### Planned Ubuntu test/build dependency additions',
        '',
        f'{len(packaging_additions)} archive-proven dependencies are absent from the selected Ubuntu packaging '
        'and will be added to the candidate source build dependencies for validation.',
        '',
    ])
    if packaging_additions:
        lines.extend(['| Consumer | Binary package | Kind | Upstream requirement |',
                      '|---|---|---|---|'])
        for source, record in packaging_additions:
            requirement = record['requirement'].replace('|', '\\|')
            lines.append(
                f"| `{source}` | `{record['binary']}` | {record['kind']} | `{requirement}` |")
    else:
        lines.append('None.')

    required = []
    for source in plan['sources']:
        for dependency, reason in sorted(entries[source].get('required_candidate_dependencies', {}).items()):
            required.append((source, dependency, reason))
    lines.extend([
        '',
        '## Dependency policy',
        '',
        f"{len(plan['archive_bootstrap_edges'])} dependency edges use Ubuntu archive packages on this first pass "
        'because the archive satisfies the upstream requirement, to break dependency cycles, or to satisfy '
        'dependencies outside an explicit pilot.',
        '',
        f'{len(required)} reviewed edges require same-run candidate packages:',
        '',
    ])
    if required:
        lines.extend(['| Consumer | Candidate dependency | Reason |', '|---|---|---|'])
        for source, dependency, reason in required:
            lines.append(f'| `{source}` | `{dependency}` | {reason.replace("|", "\\|")} |')
    else:
        lines.append('None.')

    failures = plan.get('resolution_failures', [])
    lines.extend(['', '## Source resolution', ''])
    if failures:
        lines.append(f'{len(failures)} source references failed to resolve:')
        lines.append('')
        for failure in failures:
            lines.append(f"- `{failure['source']}`: {failure['error']}")
    else:
        lines.append('All selected source references resolved to immutable commit SHAs.')
    fallbacks = plan.get('packaging_source_fallbacks', [])
    ubuntu_git = sum(1 for source in plan['sources']
                     if entries[source].get('packaging_source_role') == 'ubuntu-openstack')
    archive_git = sum(1 for source in plan['sources']
                      if entries[source].get('packaging_source_role') == 'archive-vcs')
    lines.extend(['', '## Packaging source', '',
                  f'{ubuntu_git} selected packages use plan-pinned Ubuntu OpenStack Launchpad Git trees. '
                  f'{archive_git} use their archive VCS repository because Launchpad has no corresponding tree.'])
    if fallbacks:
        lines.append(
            f'{len(fallbacks)} packages require the published Ubuntu source fallback:')
        lines.append('')
        for fallback in fallbacks:
            lines.append(f"- `{fallback['source']}`: {fallback['error']}")
    else:
        lines.append('No selected package requires archive packaging extraction.')
    return '\n'.join(lines) + '\n'


def dependency_level_matrix(waves: list[list[str]]) -> dict:
    """Return only real dependency levels for a dynamic workflow matrix."""
    return {'include': [{'level': index,
                         'packages': json.dumps(packages, separators=(',', ':'))}
                        for index, packages in enumerate(waves, 1) if packages]}


def render_discovery_summary(catalog: dict, sources: list[str], roots: list[str]) -> str:
    entries = {entry['source']: entry for entry in catalog['packages']}
    lines = [f"# OpenStack {catalog.get('series', 'unknown')} snapshot source discovery", '',
             f'{len(sources)} source packages were selected and pinned for parallel source preparation.', '',
             f"Series status: `{catalog.get('series_status', 'unknown')}`", '',
             f"Requested roots: {', '.join(f'`{source}`' for source in roots)}", '',
             '| Source | Selection reason | Upstream selection | Upstream SHA | Packaging SHA |',
             '|---|---|---|---|---|']
    for source in sources:
        entry = entries[source]
        reasons = ', '.join(entry.get('selection_reasons', [])) or 'catalog selection'
        selection = f"`{entry.get('upstream_ref', 'unknown')}` ({entry.get('branch_policy', 'unknown')})"
        if entry.get('upstream_release'):
            selection += f"; release `{entry['upstream_release']}`"
        lines.append(f"| `{source}` | {reasons} | {selection} | "
                     f"`{entry.get('upstream_sha', 'unresolved')}` | "
                     f"`{entry.get('packaging_sha', 'archive fallback')}` |")
    lines.extend(['', 'Dependency levels will be computed after these exact sources produce their `.dsc` metadata.'])
    return '\n'.join(lines) + '\n'


def freeze_packaging(entry):
    """Prefer Ubuntu OpenStack Git, then archive VCS, then published source."""
    ubuntu_repository = entry.get('packaging_repository')
    ubuntu_branches = entry.get('packaging_branch_candidates') or []
    upstream_branches = entry.get('packaging_upstream_branch_candidates') or []
    pristine_branch = entry.get('packaging_pristine_tar_branch')

    def resolve(repository, branches, role):
        if not repository or not repository.startswith('https://'):
            raise ValueError(f'No usable {role} packaging repository')
        refs = [*(f'refs/heads/{branch}' for branch in branches),
                *(f'refs/heads/{branch}' for branch in upstream_branches)]
        if pristine_branch:
            refs.append(f'refs/heads/{pristine_branch}')
        result = subprocess.run(
            ['git', 'ls-remote', '--exit-code', repository, *dict.fromkeys(refs)],
            text=True, capture_output=True, timeout=90,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        if result.returncode:
            raise ValueError(result.stderr.strip().splitlines()[-1]
                             if result.stderr.strip() else f'git ls-remote exited {result.returncode}')
        resolved = {ref.removeprefix('refs/heads/'): sha
                    for line in result.stdout.splitlines()
                    for sha, ref in [line.split()]
                    if re.fullmatch(r'[0-9a-f]{40}', sha) and ref.startswith('refs/heads/')}
        branch = next((candidate for candidate in branches if candidate in resolved), None)
        if branch is None:
            raise ValueError(f'None of the packaging branches exist: {branches}')
        upstream_branch = next((candidate for candidate in upstream_branches
                                if candidate in resolved), None)
        entry.update(packaging_source_kind='git', packaging_source_role=role,
                     packaging_source_repository=repository, packaging_branch=branch,
                     packaging_sha=resolved[branch],
                     packaging_upstream_branch=upstream_branch,
                     packaging_upstream_sha=resolved.get(upstream_branch),
                     packaging_pristine_tar_sha=resolved.get(pristine_branch))
        entry.pop('packaging_resolution_error', None)
        return

    errors = []
    try:
        resolve(ubuntu_repository, ubuntu_branches, 'ubuntu-openstack')
        return entry
    except Exception as exc:
        errors.append(f'Ubuntu OpenStack Git: {exc}')
    archive_repository = entry.get('archive_packaging_repository')
    if archive_repository and archive_repository != ubuntu_repository:
        archive_branches = list(dict.fromkeys(filter(None, [
            entry.get('archive_packaging_branch'), 'debian/hibiscus',
            'debian/unstable', 'master'])))
        try:
            resolve(archive_repository, archive_branches, 'archive-vcs')
            return entry
        except Exception as exc:
            errors.append(f'Archive VCS: {exc}')
    # Published source remains an explicit compatibility fallback when neither
    # source-package Git repository can be resolved.
    entry.update(packaging_source_kind='archive', packaging_source_role='published-source',
                 packaging_source_repository=None, packaging_branch=None,
                 packaging_sha=None, packaging_upstream_branch=None,
                 packaging_upstream_sha=None, packaging_pristine_tar_sha=None,
                 packaging_resolution_error='; '.join(errors))
    return entry


def freeze(entry):
    ref = entry['upstream_ref']
    repository = entry['upstream_repository']
    try:
        if re.fullmatch(r'[0-9a-f]{40}', ref):
            # Reviewed immutable source selection (e.g. a retired dependency).
            # prepare_source verifies the object exists in its full clone.
            entry['upstream_sha'] = ref
            entry.pop('upstream_resolution_error', None)
            return freeze_packaging(entry)
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
    return freeze_packaging(entry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path('nightly-plan/catalog-input.json'))
    parser.add_argument('--output', type=Path, default=Path('nightly-plan'))
    parser.add_argument('--sources', default='', help='Comma-separated pilot sources; omitted selects entire catalog')
    parser.add_argument('--no-resolve', action='store_true', help='Offline graph inspection only; not a buildable frozen catalog')
    parser.add_argument('--discovery-only', action='store_true',
                        help='Freeze and select sources; defer dependency levels until source preparation')
    parser.add_argument('--max-waves', type=int, default=256)
    parser.add_argument('--candidate-dependencies', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'config/hibiscus-candidate-dependencies.json')
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
        counts = {phase: dict(sorted(Counter(row[phase]['result'] for row in rows).items()))
                  for phase in ('build', 'autopkgtest')}
        (args.output / 'counts.json').write_text(json.dumps(counts, indent=2) + '\n')
        table = f'{len(rows)} requested sources. Regress-stack was not run.\n\n'
        table += '| Phase | Results |\n|---|---|\n'
        for phase, outcomes in counts.items():
            table += f"| {phase} | " + ', '.join(f'{status}: {count}' for status, count in outcomes.items()) + ' |\n'
        table += '\nSUPERFICIAL, SKIP, NO_TESTS, BLOCKED, and MISSING do not count as substantive test passes.\n\n'
        table += '| Source | Build | Autopkgtest |\n|---|---|---|\n' + ''.join(f"| {row['source']} | {row['build']['result']} | {row['autopkgtest']['result']} |\n" for row in rows)
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
    constraints = json.loads(args.candidate_dependencies.read_text())
    catalog = json.loads(args.catalog.read_text())
    requested = [s.strip() for s in args.sources.split(',') if s.strip()] or None
    dependency_errors = []
    if not args.no_resolve:
        catalog, selected, roots, dependency_errors = resolve_upstream_dependency_closure(
            catalog, requested, constraints)
    else:
        selected = set(requested) if requested else None
        if selected is not None:
            while True:
                additions = {constraint['dependency'] for constraint in constraints
                             if constraint['source'] in selected} - selected
                if not additions:
                    break
                selected.update(additions)
            selected = sorted(selected)
        roots = requested or [entry['source'] for entry in catalog['packages']]
    if args.discovery_only:
        entries = {entry['source']: entry for entry in catalog['packages']}
        selected = sorted(selected if selected is not None else entries)
        catalog['packages'] = [entries[source] for source in selected]
        plan = {'sources': selected, 'waves': [], 'archive_bootstrap_edges': [],
                'cycle_components': [], 'phase': 'source-discovery'}
    else:
        catalog, plan = plan_catalog(catalog, selected, args.max_waves, constraints)
    plan['requested_sources'] = sorted(roots)
    catalog['requested_sources'] = sorted(roots)
    catalog['resolved_at'] = datetime.now(timezone.utc).isoformat()
    catalog['ci'] = {'run_id': os.getenv('GITHUB_RUN_ID', 'local'), 'run_attempt': os.getenv('GITHUB_RUN_ATTEMPT', '1')}
    args.output.mkdir(parents=True, exist_ok=True)
    content = json.dumps(catalog, indent=2) + '\n'
    (args.output / 'catalog.json').write_text(content)
    plan['catalog_sha256'] = hashlib.sha256(content.encode()).hexdigest()
    plan['resolution_failures'] = ([{'source': p['source'], 'error': p['upstream_resolution_error']}
                                    for p in catalog['packages'] if p.get('upstream_resolution_error')]
                                   + dependency_errors)
    plan['packaging_source_fallbacks'] = [
        {'source': p['source'], 'error': p['packaging_resolution_error']}
        for p in catalog['packages'] if p.get('packaging_source_kind') == 'archive']
    (args.output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    summary = (render_discovery_summary(catalog, plan['sources'], plan['requested_sources'])
               if args.discovery_only else render_plan_summary(plan, catalog))
    (args.output / 'summary.md').write_text(summary)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
            handle.write(summary)
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
            handle.write('sources=' + json.dumps({'source': plan['sources']}, separators=(',', ':')) + '\n')
            levels = dependency_level_matrix(plan['waves'])
            handle.write('dependency_levels=' + json.dumps(levels, separators=(',', ':')) + '\n')
    print(json.dumps({'packages': len(plan['sources']), 'waves': [len(w) for w in plan['waves']],
                      'archive_bootstrap_edges': len(plan['archive_bootstrap_edges']), 'resolution_failures': len(plan['resolution_failures'])}))

if __name__ == '__main__':
    main()
