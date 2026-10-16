"""Freeze upstream refs once and plan independent snapshot build waves."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packagetest.config import default_config_path

# Catalog records retain archive and upstream evidence with evolving JSON fields.
# Keep that extensibility at the boundary; graph state has concrete types.
Catalog = dict[str, Any]
CandidateRule = dict[str, str]


DEBIAN_PACKAGING_FALLBACK_WARNING = (
    "No usable Ubuntu-maintained packaging or exact Ubuntu Cloud Archive PPA "
    "source package was available. Debian packaging is being used as the final "
    "fallback and requires human review."
)


def is_uca_ppa_source(entry: dict) -> bool:
    """Return whether the frozen source descriptor comes from a UCA PPA."""
    url = entry.get('archive_source', {}).get('url', '')
    parsed = urlparse(url)
    return (parsed.hostname in {'ppa.launchpad.net', 'ppa.launchpadcontent.net'}
            and parsed.path.startswith('/ubuntu-cloud-archive/'))


class SourceDiscovery:
    """Resolve the upstream closure using injectable ref and metadata readers.

    Create one discovery object per run; each source is pinned and inspected once. Only the private catalog copy is
    enriched; scheduling can subsequently operate without network access.
    """

    def __init__(
        self,
        catalog: Catalog,
        sources: Iterable[str] | None,
        candidate_dependencies: Iterable[CandidateRule],
        *,
        freeze_entry: Callable[[Catalog], Catalog],
        inspect_entry: Callable[[Catalog], tuple[list[Catalog], list[str]]],
    ) -> None:
        from packagetest.upstream_dependencies import (
            distribution_archive_index,
            distribution_source_index,
        )

        self.catalog = json.loads(json.dumps(catalog))
        self.entries = {entry["source"]: entry for entry in self.catalog["packages"]}
        self.requested = set(sources) if sources else set(self.entries)
        if self.requested - self.entries.keys():
            raise ValueError(
                f"Unknown requested sources: {sorted(self.requested - self.entries.keys())}"
            )
        self.selected = set(self.requested)
        self.reasons = {source: {"requested"} for source in self.requested}
        self.index = distribution_source_index(self.catalog["packages"])
        self.archive_index = distribution_archive_index(
            self.catalog.get("archive_python_packages", [])
        )
        self.constraints = tuple(candidate_dependencies)
        self.freeze_entry = freeze_entry
        self.inspect_entry = inspect_entry

    def discover(self) -> tuple[Catalog, list[str], list[str], list[dict[str, str]]]:
        inspected: set[str] = set()
        while True:
            self._select_required_candidates()
            frontier = sorted(self.selected - inspected)
            if not frontier:
                break
            with ThreadPoolExecutor(max_workers=16) as workers:
                frozen = list(
                    workers.map(
                        self.freeze_entry, (self.entries[source] for source in frontier)
                    )
                )
            for entry in frozen:
                self.entries[entry["source"]] = entry
            with ThreadPoolExecutor(max_workers=16) as workers:
                results = list(workers.map(self._inspect, frontier))
            for source, records, files, error in results:
                inspected.add(source)
                if error:
                    self.entries[source]["upstream_dependency_resolution_error"] = error
                else:
                    self._record_requirements(source, records, files)
        for source, entry in self.entries.items():
            entry["selection_reasons"] = sorted(self.reasons.get(source, []))
        self.catalog["packages"] = [
            self.entries[source] for source in sorted(self.entries)
        ]
        errors = [
            {
                "source": source,
                "error": self.entries[source]["upstream_dependency_resolution_error"],
            }
            for source in sorted(self.selected)
            if self.entries[source].get("upstream_dependency_resolution_error")
        ]
        return self.catalog, sorted(self.selected), sorted(self.requested), errors

    def _select_required_candidates(self) -> None:
        for rule in self.constraints:
            if (
                rule["source"] in self.selected
                and rule["dependency"] not in self.selected
            ):
                self.selected.add(rule["dependency"])
                self.reasons.setdefault(rule["dependency"], set()).add(
                    f"mandatory candidate for {rule['source']}"
                )

    def _inspect(self, source: str) -> tuple[str, list[Catalog], list[str], str | None]:
        entry = self.entries[source]
        if entry.get("upstream_resolution_error"):
            return source, [], [], entry["upstream_resolution_error"]
        try:
            records, files = self.inspect_entry(entry)
            return source, records, files, None
        except Exception as exc:
            # One unavailable project must remain visible as evidence without
            # preventing independent projects from being discovered.
            return source, [], [], str(exc)

    def _record_requirements(
        self, source: str, records: list[Catalog], files: list[str]
    ) -> None:
        from packagetest.catalog import dependency_names
        from packagetest.upstream_dependencies import map_requirements

        entry = self.entries[source]
        mapped, dependencies = map_requirements(
            records, self.index, source, self.entries, self.archive_index
        )
        entry["upstream_dependency_files"] = files
        entry["upstream_dependency_requirements"] = mapped
        entry["upstream_dependencies"] = dependencies
        packaged = dependency_names(entry.get("build_depends", ""))
        entry["planned_archive_dependency_additions"] = [
            {
                "binary": record["archive_binary"],
                "source": record["archive_source"],
                "archive_version": record["archive_version"],
                "kind": record["kind"],
                "requirement": record["requirement"],
                "file": record["file"],
                "line": record["line"],
            }
            for record in mapped
            if record.get("kind") in {"test", "build-system"}
            and record.get("archive_binary")
            and record["archive_binary"] not in packaged
            and record.get("archive_satisfies") is True
        ]
        entry["archive_satisfied_upstream_dependencies"] = sorted(
            {
                record["source"]
                for record in mapped
                if record.get("source")
                and record.get("archive_satisfies") is True
                and record.get("source") not in dependencies
            }
        )
        entry.pop("upstream_dependency_resolution_error", None)
        entry["packaging_build_dependencies"] = list(
            entry.get("build_dependencies", [])
        )
        entry["build_dependencies"] = sorted(
            set(entry.get("build_dependencies", [])) | set(dependencies)
        )
        for dependency in dependencies:
            self.reasons.setdefault(dependency, set()).add(
                f"upstream dependency of {source}"
            )
            self.selected.add(dependency)


def resolve_upstream_dependency_closure(
    catalog: Catalog,
    sources: Iterable[str] | None = None,
    candidate_dependencies: Iterable[CandidateRule] = (),
    freeze_entry: Callable[[Catalog], Catalog] | None = None,
    inspect_entry: Callable[[Catalog], tuple[list[Catalog], list[str]]] | None = None,
) -> tuple[Catalog, list[str], list[str], list[dict[str, str]]]:
    """Freeze roots and recursively add their upstream Python dependencies."""
    from packagetest.upstream_dependencies import inspect_revision

    return SourceDiscovery(
        catalog,
        sources,
        candidate_dependencies,
        freeze_entry=freeze_entry or freeze,
        inspect_entry=inspect_entry or inspect_revision,
    ).discover()


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


class CatalogScheduler:
    """Own graph state for one scheduling pass over a private catalog copy."""

    def __init__(self, catalog: Catalog, sources: Iterable[str] | None = None) -> None:
        self.catalog = json.loads(json.dumps(catalog))
        self.entries = {entry["source"]: entry for entry in self.catalog["packages"]}
        if len(self.entries) != len(self.catalog["packages"]):
            raise ValueError("Duplicate catalog sources")
        self.selected = set(sources) if sources else set(self.entries)
        if self.selected - self.entries.keys():
            raise ValueError(
                f"Unknown requested sources: {sorted(self.selected - self.entries.keys())}"
            )
        self.graph: dict[str, set[str]] = {
            source: set(self.entries[source]["build_dependencies"]) & self.selected
            for source in self.selected
        }
        self.groups = components(self.graph)
        self.bootstrap: list[Catalog] = []

    def schedule(
        self, max_waves: int = 12, candidate_dependencies: Iterable[CandidateRule] = ()
    ) -> tuple[Catalog, Catalog]:
        self._bootstrap_outside_selection()
        self._break_cycles()
        self._require_candidates(candidate_dependencies)
        waves = self._waves(max_waves)
        self._reuse_earlier_candidates(waves)
        for index, wave in enumerate(waves):
            for source in wave:
                entry = self.entries[source]
                entry["run_dependencies"] = sorted(self.graph[source])
                entry["archive_bootstrap_dependencies"] = sorted(
                    edge["dependency"]
                    for edge in self.bootstrap
                    if edge["source"] == source
                )
                entry["wave"] = index
        self.catalog["packages"] = [
            self.entries[source] for source in sorted(self.selected)
        ]
        return self.catalog, {
            "sources": sorted(self.selected),
            "waves": waves,
            "archive_bootstrap_edges": self.bootstrap,
            "cycle_components": [group for group in self.groups if len(group) > 1],
        }

    def _bootstrap_outside_selection(self) -> None:
        for source in sorted(self.graph):
            entry = self.entries[source]
            outside = set(entry["build_dependencies"]) - self.selected
            for dependency in sorted(outside):
                satisfied = dependency in entry.get(
                    "archive_satisfied_upstream_dependencies", []
                )
                self.bootstrap.append(
                    {
                        "source": source,
                        "dependency": dependency,
                        "reason": (
                            "Ubuntu archive satisfies upstream requirement"
                            if satisfied
                            else "outside explicitly selected pilot"
                        ),
                    }
                )

    def _break_cycles(self) -> None:
        for group in self.groups:
            if len(group) == 1 and group[0] not in self.graph[group[0]]:
                continue
            members = set(group)
            for source in group:
                for dependency in sorted(self.graph[source] & members):
                    self.graph[source].remove(dependency)
                    self.bootstrap.append(
                        {
                            "source": source,
                            "dependency": dependency,
                            "reason": "dependency cycle bootstrap",
                            "component": group,
                        }
                    )

    def _require_candidates(self, constraints: Iterable[CandidateRule]) -> None:
        # Reviewed API requirements override bootstrap choices, including SCC edges.
        for constraint in constraints:
            source, dependency = constraint["source"], constraint["dependency"]
            if source not in self.entries or dependency not in self.entries:
                raise ValueError(
                    f"Unknown mandatory candidate dependency: {source} -> {dependency}"
                )
            if dependency not in self.entries[source]["build_dependencies"]:
                raise ValueError(
                    f"Mandatory candidate is not a declared build dependency: {source} -> {dependency}"
                )
            if source not in self.selected:
                continue
            if dependency not in self.selected:
                raise ValueError(
                    f"{source} requires candidate {dependency}; include it in the selected sources"
                )
            self.graph[source].add(dependency)
            self.bootstrap = [
                edge
                for edge in self.bootstrap
                if (edge["source"], edge["dependency"]) != (source, dependency)
            ]
            self.entries[source].setdefault("required_candidate_dependencies", {})[
                dependency
            ] = constraint["reason"]

    def _waves(self, max_waves: int) -> list[list[str]]:
        completed: set[str] = set()
        waves: list[list[str]] = []
        while len(completed) < len(self.graph):
            wave = sorted(
                source
                for source, dependencies in self.graph.items()
                if source not in completed and dependencies <= completed
            )
            if not wave:
                raise ValueError("Unresolved dependency cycle")
            waves.append(wave)
            completed.update(wave)
        if len(waves) > max_waves:
            raise ValueError(
                f"{len(waves)} dependency waves exceed workflow capacity {max_waves}"
            )
        return waves

    def _reuse_earlier_candidates(self, waves: list[list[str]]) -> None:
        # Restore only edges to earlier waves: candidate reuse adds no serialization.
        order = {source: index for index, wave in enumerate(waves) for source in wave}
        remaining = []
        for edge in self.bootstrap:
            if (
                edge["reason"] == "dependency cycle bootstrap"
                and order[edge["dependency"]] < order[edge["source"]]
            ):
                self.graph[edge["source"]].add(edge["dependency"])
            else:
                remaining.append(edge)
        self.bootstrap = remaining


def plan_catalog(
    catalog: Catalog,
    sources: Iterable[str] | None = None,
    max_waves: int = 12,
    candidate_dependencies: Iterable[CandidateRule] = (),
) -> tuple[Catalog, Catalog]:
    """Plan one catalog; source resolution and publication are separate stages."""
    return CatalogScheduler(catalog, sources).schedule(
        max_waves, candidate_dependencies
    )


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
            escaped_reason = reason.replace("|", "\\|")
            lines.append(f'| `{source}` | `{dependency}` | {escaped_reason} |')
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
    importer_git = sum(1 for source in plan['sources']
                       if entries[source].get('packaging_source_role') == 'ubuntu-importer')
    archive_git = sum(1 for source in plan['sources']
                      if entries[source].get('packaging_source_role') in {
                          'series-archive-vcs', 'archive-vcs'})
    ppa_source = sum(1 for source in plan['sources']
                     if entries[source].get('packaging_source_role') == 'uca-ppa-source')
    lines.extend(['', '## Packaging source', '',
                  f'{ubuntu_git} selected packages use plan-pinned Ubuntu OpenStack Launchpad Git trees. '
                  f'{importer_git} use plan-pinned Ubuntu importer Git trees. '
                  f'{ppa_source} use exact checksummed Ubuntu Cloud Archive PPA source packages. '
                  f'{archive_git} use their archive VCS repository because Launchpad has no corresponding tree.'])
    if fallbacks:
        lines.append(
            f'{len(fallbacks)} packages require the published Ubuntu source fallback:')
        lines.append('')
        for fallback in fallbacks:
            lines.append(f"- `{fallback['source']}`: {fallback['error']}")
    else:
        lines.append('No selected package requires archive packaging extraction.')
    warnings = [(source, entries[source]['packaging_fallback_warning'])
                for source in plan['sources']
                if entries[source].get('packaging_fallback_warning')]
    if warnings:
        lines.extend(['', '## Packaging fallback warnings', '',
                      'These packages use Debian packaging as the final fallback and require human review:', ''])
        for source, warning in warnings:
            lines.append(f'- **WARNING — `{source}`:** {warning}')
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
             '| Source | Selection reason | Archive comparison | Upstream selection | Upstream SHA | Packaging SHA |',
             '|---|---|---|---|---|---|']
    for source in sources:
        entry = entries[source]
        reasons = ', '.join(entry.get('selection_reasons', [])) or 'catalog selection'
        selection = f"`{entry.get('upstream_ref', 'unknown')}` ({entry.get('branch_policy', 'unknown')})"
        if entry.get('upstream_release'):
            selection += f"; release `{entry['upstream_release']}`"
        comparison = entry.get('comparison_archive_version')
        pocket = entry.get('comparison_archive_pocket')
        baseline = (f"`{comparison}` from `{pocket}`" if comparison
                    else 'No target-archive package')
        lines.append(f"| `{source}` | {reasons} | {baseline} | {selection} | "
                     f"`{entry.get('upstream_sha', 'unresolved')}` | "
                     f"`{entry.get('packaging_sha', 'archive fallback')}` |")
    warnings = [(source, entries[source]['packaging_fallback_warning'])
                for source in sources
                if entries[source].get('packaging_fallback_warning')]
    if warnings:
        lines.extend(['', '## Packaging fallback warnings', '',
                      'These packages use Debian packaging as the final fallback and require human review:', ''])
        for source, warning in warnings:
            lines.append(f'- **WARNING — `{source}`:** {warning}')
    lines.extend(['', 'Dependency levels will be computed after these exact sources produce their `.dsc` metadata.'])
    return '\n'.join(lines) + '\n'


def freeze_packaging(entry):
    """Prefer Ubuntu Git, then an exact UCA PPA source, then Debian Git."""
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
        entry.pop('packaging_fallback_warning', None)
        return

    errors = []
    try:
        resolve(ubuntu_repository, ubuntu_branches, 'ubuntu-openstack')
        return entry
    except Exception as exc:
        errors.append(f'Ubuntu OpenStack Git: {exc}')
    importer_repository = entry.get('ubuntu_importer_repository')
    importer_branches = entry.get('ubuntu_importer_branch_candidates') or []
    if importer_repository and importer_repository != ubuntu_repository:
        try:
            resolve(importer_repository, importer_branches, 'ubuntu-importer')
            return entry
        except Exception as exc:
            errors.append(f'Ubuntu importer Git: {exc}')
    # The PPA source is the exact, checksummed package selected for this UCA
    # run. Prefer it over Debian VCS packaging, whose branch can differ from
    # the Ubuntu/UCA package even when its upstream release name matches.
    if is_uca_ppa_source(entry):
        entry.update(packaging_source_kind='archive',
                     packaging_source_role='uca-ppa-source',
                     packaging_source_repository=None, packaging_branch=None,
                     packaging_sha=None, packaging_upstream_branch=None,
                     packaging_upstream_sha=None,
                     packaging_pristine_tar_sha=None,
                     packaging_resolution_error='; '.join(errors))
        entry.pop('packaging_fallback_warning', None)
        return entry
    archive_repository = entry.get('archive_packaging_repository')
    # When Ubuntu has no usable packaging tree, prefer the Debian branch for
    # the exact OpenStack series before considering moving generic branches.
    series_archive_branches = [entry.get(
        'archive_packaging_series_branch', 'debian/hibiscus')]
    if archive_repository and archive_repository not in {
            ubuntu_repository, importer_repository}:
        try:
            resolve(archive_repository, series_archive_branches,
                    'series-archive-vcs')
            entry['packaging_fallback_warning'] = DEBIAN_PACKAGING_FALLBACK_WARNING
            return entry
        except Exception as exc:
            errors.append(f'Series archive VCS: {exc}')
    if archive_repository and archive_repository not in {ubuntu_repository, importer_repository}:
        archive_branches = list(dict.fromkeys(filter(None, [
            entry.get('archive_packaging_branch'),
            'debian/unstable', 'master'])))
        try:
            resolve(archive_repository, archive_branches, 'archive-vcs')
            entry['packaging_fallback_warning'] = DEBIAN_PACKAGING_FALLBACK_WARNING
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
    url = entry.get('archive_source', {}).get('url', '')
    if 'debian.org' in (urlparse(url).hostname or ''):
        entry['packaging_fallback_warning'] = DEBIAN_PACKAGING_FALLBACK_WARNING
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


def publish_job_summary(args: argparse.Namespace) -> None:
    catalog = json.loads(args.catalog.read_text())
    sources = [p["source"] for p in catalog["packages"]]
    prefix = catalog.get("target", {}).get("id")
    prefix = f"{prefix}-" if prefix else ""
    rows = []
    for source in sources:
        row = {"source": source}
        for phase in ("build", "autopkgtest"):
            paths = list(
                (args.summary_inputs / f"status-{prefix}{phase}-{source}").rglob(
                    "result.json"
                )
            )
            row[phase] = (
                json.loads(paths[0].read_text())
                if len(paths) == 1
                else {
                    "result": "MISSING",
                    "error": "Job produced no unique status report",
                }
            )
        rows.append(row)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    counts = {
        phase: dict(sorted(Counter(row[phase]["result"] for row in rows).items()))
        for phase in ("build", "autopkgtest")
    }
    (args.output / "counts.json").write_text(json.dumps(counts, indent=2) + "\n")
    table = f"{len(rows)} requested sources. Regress-stack was not run.\n\n"
    table += "| Phase | Results |\n|---|---|\n"
    for phase, outcomes in counts.items():
        table += (
            f"| {phase} | "
            + ", ".join(f"{status}: {count}" for status, count in outcomes.items())
            + " |\n"
        )
    table += "\nSUPERFICIAL, SKIP, NO_TESTS, BLOCKED, and MISSING do not count as substantive test passes.\n\n"
    table += "| Source | Build | Autopkgtest |\n|---|---|---|\n" + "".join(
        f"| {row['source']} | {row['build']['result']} | {row['autopkgtest']['result']} |\n"
        for row in rows
    )
    (args.output / "summary.md").write_text(table)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(table)
    print(table)


def emit_dependency_pattern(args: argparse.Namespace) -> None:
    catalog = json.loads(args.catalog.read_text())
    entries = {p["source"]: p for p in catalog["packages"]}
    prefix = catalog.get("target", {}).get("id")
    prefix = f"{prefix}-" if prefix else ""
    needed = set()

    def add(source):
        for dep in entries[source].get("run_dependencies", []):
            if dep not in needed:
                needed.add(dep)
                add(dep)

    add(args.dependency_pattern)
    if args.include_self:
        needed.add(args.dependency_pattern)
    names = [f"build-{prefix}{source}" for source in sorted(needed)]
    pattern = (
        names[0]
        if len(names) == 1
        else "{" + ",".join(names) + "}"
        if names
        else "__no_dependencies__"
    )
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write("pattern=" + pattern + "\n")
            handle.write("needed=" + ("true" if names else "false") + "\n")
    print(pattern)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, default=Path("nightly-plan/catalog-input.json")
    )
    parser.add_argument("--output", type=Path, default=Path("nightly-plan"))
    parser.add_argument(
        "--sources",
        default="",
        help="Comma-separated pilot sources; omitted selects entire catalog",
    )
    parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="Offline graph inspection only; not a buildable frozen catalog",
    )
    parser.add_argument(
        "--discovery-only",
        action="store_true",
        help="Freeze and select sources; defer dependency levels until source preparation",
    )
    parser.add_argument("--max-waves", type=int, default=256)
    parser.add_argument(
        "--candidate-dependencies",
        type=Path,
        default=default_config_path("hibiscus-candidate-dependencies.json"),
    )
    parser.add_argument(
        "--dependency-pattern",
        help="Emit artifact download pattern for one frozen catalog source",
    )
    parser.add_argument("--include-self", action="store_true")
    parser.add_argument("--summary-inputs", type=Path)
    args = parser.parse_args()
    if args.summary_inputs:
        publish_job_summary(args)
    elif args.dependency_pattern:
        emit_dependency_pattern(args)
    else:
        run(args)


def offline_selection(
    requested: list[str] | None, constraints: Iterable[CandidateRule]
) -> list[str] | None:
    """Expand mandatory candidates without network access or upstream inspection."""
    if not requested:
        return None
    selected = set(requested)
    constraints = tuple(constraints)
    while True:
        additions = {
            rule["dependency"] for rule in constraints if rule["source"] in selected
        } - selected
        if not additions:
            return sorted(selected)
        selected.update(additions)


def run(args: argparse.Namespace) -> None:
    """Coordinate discovery, scheduling, and publication for one invocation."""
    constraints = json.loads(args.candidate_dependencies.read_text())
    catalog = json.loads(args.catalog.read_text())
    requested = [s.strip() for s in args.sources.split(",") if s.strip()] or None
    dependency_errors = []
    if not args.no_resolve:
        catalog, selected, roots, dependency_errors = (
            resolve_upstream_dependency_closure(catalog, requested, constraints)
        )
    else:
        selected = offline_selection(requested, constraints)
        roots = requested or [entry["source"] for entry in catalog["packages"]]
    if args.discovery_only:
        entries = {entry["source"]: entry for entry in catalog["packages"]}
        selected = sorted(selected if selected is not None else entries)
        catalog["packages"] = [entries[source] for source in selected]
        plan = {
            "sources": selected,
            "waves": [],
            "archive_bootstrap_edges": [],
            "cycle_components": [],
            "phase": "source-discovery",
        }
    else:
        catalog, plan = plan_catalog(catalog, selected, args.max_waves, constraints)
    PlanPublisher(args.output).publish(
        catalog, plan, roots, dependency_errors, discovery_only=args.discovery_only
    )


@dataclass(frozen=True)
class PlanPublisher:
    """Serialize plan evidence and render reports without choosing graph edges."""

    output: Path

    def publish(
        self,
        catalog: Catalog,
        plan: Catalog,
        roots: list[str],
        dependency_errors: list[dict[str, str]],
        *,
        discovery_only: bool,
    ) -> None:
        plan["requested_sources"] = sorted(roots)
        catalog["requested_sources"] = sorted(roots)
        catalog["resolved_at"] = datetime.now(timezone.utc).isoformat()
        catalog["ci"] = {
            "run_id": os.getenv("GITHUB_RUN_ID", "local"),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "1"),
        }
        self.output.mkdir(parents=True, exist_ok=True)
        content = json.dumps(catalog, indent=2) + "\n"
        (self.output / "catalog.json").write_text(content)
        plan["catalog_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        plan["resolution_failures"] = [
            {"source": p["source"], "error": p["upstream_resolution_error"]}
            for p in catalog["packages"]
            if p.get("upstream_resolution_error")
        ] + dependency_errors
        plan["packaging_source_fallbacks"] = [
            {"source": p["source"], "error": p["packaging_resolution_error"]}
            for p in catalog["packages"]
            if p.get("packaging_source_kind") == "archive"
        ]
        (self.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        summary = (
            render_discovery_summary(
                catalog, plan["sources"], plan["requested_sources"]
            )
            if discovery_only
            else render_plan_summary(plan, catalog)
        )
        (self.output / "summary.md").write_text(summary)
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
                handle.write(summary)
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                handle.write(
                    "sources="
                    + json.dumps({"source": plan["sources"]}, separators=(",", ":"))
                    + "\n"
                )
                levels = dependency_level_matrix(plan["waves"])
                handle.write(
                    "dependency_levels="
                    + json.dumps(levels, separators=(",", ":"))
                    + "\n"
                )
        print(
            json.dumps(
                {
                    "packages": len(plan["sources"]),
                    "waves": [len(w) for w in plan["waves"]],
                    "archive_bootstrap_edges": len(plan["archive_bootstrap_edges"]),
                    "resolution_failures": len(plan["resolution_failures"]),
                }
            )
        )


if __name__ == "__main__":
    main()
