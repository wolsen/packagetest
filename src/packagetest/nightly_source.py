"""Prepare independently buildable, pinned snapshot source packages.

The Ubuntu OpenStack Launchpad Git tree normally supplies Debian packaging.
For UCA targets, the exact checksum-pinned PPA source is preferred when those
Ubuntu trees are unavailable or stale. Debian VCS packaging is the final
fallback. Upstream Git supplies the new source tree.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
import difflib
import fnmatch
import gzip
import hashlib
import io
import json
import os
import random
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
import tomlkit
from tomlkit.exceptions import ParseError as TOMLKitParseError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .artifacts import checksum_entries, fields, sha256, verify_source
from .catalog import dependency_names
from .commands import CommandExecutor, CommandRunner
from .patches import already_applied_patches, integrated_upstream_patches, superseded_upstream_patches
from .snapshot import build_snapshot
from .snapshot_lock import select_commit

BUILD_REQUIREMENTS = [
    {'name': 'pbr', 'version': '7.0.3', 'sha256': 'ff223894eb1cd271a98076b13d3badff3bb36c424074d26334cd25aebeecea6b'},
    {'name': 'setuptools', 'version': '80.9.0', 'sha256': '062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922'},
    {'name': 'wheel', 'version': '0.45.1', 'sha256': '708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248'},
]
EXECUTABLE_ENTRY_POINT_GROUPS = frozenset({'console_scripts', 'gui_scripts', 'wsgi_scripts'})

SCM_REQUIREMENTS = [
    {'name': 'setuptools-scm', 'version': '8.3.1', 'sha256': '332ca0d43791b818b841213e76b1971b7711a960761c5bea5fc5cdb5196fbce3'},
    {'name': 'packaging', 'version': '25.0', 'sha256': '29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484'},
]


class PackagingIdentityMismatch(ValueError):
    """A generic packaging Git branch does not describe the archive package."""


LAUNCHPAD_FETCH_ATTEMPTS = 5
LAUNCHPAD_FETCH_BACKOFF = (10, 20, 40, 80)
_PERMANENT_LAUNCHPAD_FETCH_ERRORS = (
    'authentication failed',
    'could not read username',
    "couldn't find remote ref",
    'does not appear to be a git repository',
    'not our ref',
    'repository not found',
    'requested url returned error: 401',
    'requested url returned error: 403',
    'requested url returned error: 404',
    'server does not allow request for unadvertised object',
)


def _is_launchpad_git(repository: str) -> bool:
    return urlparse(repository).hostname == 'git.launchpad.net'


def _is_uca_ppa_source(entry: dict) -> bool:
    url = entry.get('archive_source', {}).get('url', '')
    parsed = urlparse(url)
    return (parsed.hostname in {'ppa.launchpad.net', 'ppa.launchpadcontent.net'}
            and parsed.path.startswith('/ubuntu-cloud-archive/'))


def _can_use_published_source_fallback(entry: dict) -> bool:
    role = entry.get('packaging_source_role')
    return (role in {'series-archive-vcs', 'archive-vcs'} or
            role == 'ubuntu-importer' and _is_uca_ppa_source(entry))


def _is_permanent_launchpad_fetch_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in _PERMANENT_LAUNCHPAD_FETCH_ERRORS)


def _entry_points_from_text(setup_cfg: str | None, pyproject: str | None) -> list[dict]:
    """Return statically declared Python entry points in a stable form."""
    result = _setup_entry_points(setup_cfg)
    result.extend(_project_entry_points(pyproject))
    unique = {(item['group'], item['name'], item['target']): item for item in result}
    return [unique[key] for key in sorted(unique)]


def _revision_file(checkout: Path, revision: str, name: str) -> str | None:
    result = subprocess.run(['git', '-C', str(checkout), 'show', f'{revision}:{name}'],
                            text=True, capture_output=True)
    return result.stdout if result.returncode == 0 else None


def _tree_entry_points(tree: Path) -> list[dict]:
    def content(name):
        path = tree / name
        return path.read_text() if path.is_file() else None
    return _entry_points_from_text(content('setup.cfg'), content('pyproject.toml'))


def _revision_entry_points(checkout: Path, revision: str) -> list[dict]:
    return _entry_points_from_text(_revision_file(checkout, revision, 'setup.cfg'),
                                   _revision_file(checkout, revision, 'pyproject.toml'))


def _install_patterns(path: Path) -> list[str]:
    patterns = []
    for line in path.read_text().splitlines():
        words = line.split('#', 1)[0].split()
        if words:
            pattern = words[0].removeprefix('debian/tmp/')
            patterns.append(pattern)
    return patterns


def _manifest_owners(debian: Path, installed_path: str) -> list[Path]:
    return sorted(path for path in debian.glob('*.install')
                  if any(fnmatch.fnmatchcase(installed_path, pattern)
                         for pattern in _install_patterns(path)))


def _archive_upstream_version(version: str) -> str:
    without_epoch = version.split(':', 1)[-1]
    return without_epoch.rsplit('-', 1)[0] if '-' in without_epoch else without_epoch


def _commit_inventory(checkout: Path, start: str, end: str) -> list[dict]:
    result = subprocess.run(
        ['git', '-C', str(checkout), 'log', '--reverse',
         '--format=%H%x00%ct%x00%an%x00%s', f'{start}..{end}'],
        text=True, capture_output=True, check=True)
    return [{'sha': values[0], 'timestamp': int(values[1]), 'author': values[2],
             'subject': values[3]}
            for line in result.stdout.splitlines() for values in [line.split('\0', 3)]]


def _official_commit_delta(entry: dict, checkout: Path, selected: dict) -> dict:
    """Prefer the release tag matching Ubuntu's packaged upstream version."""
    comparison_version = entry.get(
        'comparison_archive_version', entry['archive_version'])
    if comparison_version is None:
        return {
            'comparison_tag': None, 'comparison_tag_sha': None,
            'archive_upstream_version': None,
            'comparison_tag_matches_archive_version': False,
            'basis': 'no-target-archive-baseline',
            'count': None, 'commits': [],
            'official_tag_resolution': 'target-archive-package-absent',
            'comparison_archive_pocket': None,
        }

    archive_upstream = _archive_upstream_version(comparison_version)
    matches = _matching_archive_tags(checkout, selected, archive_upstream)
    if len(matches) == 1:
        tag = matches[0]
        tag_sha = subprocess.check_output(
            ['git', '-C', str(checkout), 'rev-parse', f'refs/tags/{tag}^{{commit}}'],
            text=True).strip()
        commits = _commit_inventory(checkout, tag_sha, selected['sha'])
        return {
            'comparison_tag': tag, 'comparison_tag_sha': tag_sha,
            'archive_upstream_version': archive_upstream,
            'comparison_tag_matches_archive_version': True,
            'basis': 'official-package-upstream-tag',
            'comparison_archive_pocket': entry.get('comparison_archive_pocket'),
            'count': len(commits), 'commits': commits,
        }

    # Some Ubuntu versions contain repacks or post-release snapshots for which
    # no unique upstream tag exists. Preserve a useful, explicitly qualified
    # delta rather than presenting the nearest tag as the official baseline.
    return {
        'comparison_tag': selected['base_tag'],
        'comparison_tag_sha': selected['base_tag_sha'],
        'archive_upstream_version': archive_upstream,
        'comparison_tag_matches_archive_version': False,
        'basis': 'nearest-upstream-release-tag',
        'comparison_archive_pocket': entry.get('comparison_archive_pocket'),
        'count': selected['commits_since_tag'],
        'commits': selected.get('commits', []),
        'official_tag_resolution': ('not-found' if not matches else 'ambiguous'),
        'matching_tags': matches,
    }


def source_evolution(entry: dict, checkout: Path, selected: dict, tree: Path) -> tuple[dict, list[dict]]:
    """Describe upstream changes and safely assign new executable entry points.

    A manifest is changed only when existing commands identify exactly one
    binary-package owner. Package splits remain an explicit human decision.
    """
    baseline = _revision_entry_points(checkout, selected['base_tag_sha'])
    candidate = _tree_entry_points(tree)
    old_keys = {(item['group'], item['name'], item['target']) for item in baseline}
    introduced = [item for item in candidate
                  if (item['group'], item['name'], item['target']) not in old_keys]
    editor = EntryPointEditor(tree / 'debian', baseline)
    findings, actions = editor.adapt(introduced)

    commit_delta = _official_commit_delta(entry, checkout, selected)
    return ({'schema_version': 1, 'source': entry['source'],
             'commit_delta': commit_delta, 'introduced_entry_points': findings,
             'human_binary_package_review_required': bool(findings)}, actions)


def write_source_evolution(report: dict, destination: Path) -> None:
    """Write machine-readable evolution evidence and its human review summary."""
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'source-evolution.json').write_text(json.dumps(report, indent=2) + '\n')
    delta = report['commit_delta']
    lines = [f"## {report['source']} upstream evolution", '']
    _evolution_baseline_lines(delta, lines)
    if delta['commits']:
        lines.extend(['', '| Commit | Subject |', '|---|---|'])
        lines.extend(f"| `{item['sha'][:12]}` | {item['subject'].replace('|', '&#124;')} |"
                     for item in delta['commits'])

    _evolution_entry_lines(report, lines)
    (destination / 'source-evolution.md').write_text('\n'.join(lines) + '\n')


class SourcePreparer:
    """Own one isolated source-preparation workspace and its failure evidence."""

    def __init__(self, destination: Path, *, runner: CommandExecutor | None = None) -> None:
        """Create a fresh destination and command log for source preparation."""
        self.root = destination.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.runner: CommandExecutor = runner if runner is not None else CommandRunner(
            self.root / 'logs' / 'commands.jsonl', stream=True, timeout=1800)

    def command(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> str:
        """Run a preparation command and return its complete captured stdout."""
        result = self.runner.run(list(args), cwd or self.work, env_diff=env)
        if result.exit_code:
            raise RuntimeError(f'{args[0]} failed ({result.exit_code}): {result.stderr[-2000:]}')

        return result.read_stdout().strip()

    def prepare(self, entry: dict, *, cutoff: str | None = None,
                remediation_patch: Path | None = None, target: dict | None = None) -> dict:
        """Prepare one source, retaining resolution and command evidence on failure."""
        report = {'source': entry['source'], 'status': 'PREPARING', 'catalog_entry': entry}
        report_path = self.root / 'resolution.json'
        try:
            self._validate_entry(entry)
            source = entry['source']
            packaging_tree, baseline_dsc, packaging_source = self._packaging_source(entry)
            report['packaging_source'] = packaging_source
            checkout, selected = self._select_upstream(entry, cutoff)
            upstream = selected['upstream_version']
            target = target or {}
            revision_suffix = target.get('debian_revision_suffix', '-0ubuntu1~hibiscus1')
            distribution = target.get('distribution', 'resolute')
            openstack_series = target.get('openstack_series', '2026.2')
            version = snapshot_debian_version(entry['archive_version'], upstream,
                                              revision_suffix)
            self.command('dpkg', '--compare-versions', version, 'gt', entry['archive_version'])
            orig = self.root / f'{source}_{upstream}.orig.tar.gz'
            snapshot = self._snapshot_archive(entry, checkout, selected, orig, source, upstream)
            tree = self.root / f'{source}-{upstream}'
            extract_snapshot(orig, tree)
            self._preserve_components(entry, baseline_dsc, source, upstream, tree, report)
            proposal_baseline = self._copy_packaging(packaging_tree, tree)
            evolution, evolution_actions = source_evolution(entry, checkout, selected, tree)
            report['source_evolution'] = evolution
            write_source_evolution(evolution, self.root / 'packaging-proposal')
            report['packaging_adjustments'] = integrated_upstream_patches(
                tree, checkout, selected['sha'])
            report['packaging_adjustments'].extend(already_applied_patches(tree))
            report['packaging_adjustments'].extend(superseded_upstream_patches(
                tree, checkout, evolution.get('commit_delta', {})))
            report['packaging_adjustments'].extend(upstream_dependency_adjustments(entry, tree))
            report['packaging_adjustments'].extend(setuptools_discovery_adjustments(tree))
            report['packaging_adjustments'].extend(autopkgtest_packaging_adjustments(entry, tree))
            report['packaging_adjustments'].extend(evolution_actions)
            self._apply_remediation(remediation_patch, tree, report)
            proposal = write_packaging_proposal(
                entry, proposal_baseline, tree, self.root / 'packaging-proposal',
                report['packaging_adjustments'])
            if proposal:
                report['packaging_proposal'] = proposal

            dscs = self._build_source_package(tree, source, version, distribution, openstack_series, selected)
            report.update(status='PREPARED', version=version, snapshot=snapshot,
                          dsc=str(dscs[0]), dsc_sha256=sha256(dscs[0]),
                          prepared_at=datetime.now(timezone.utc).isoformat())
            report_path.write_text(json.dumps(report, indent=2) + '\n')
            return {'dsc': str(dscs[0]), 'tree': str(tree),
                    'metadata': report, 'lock': str(report_path)}
        except Exception as error:
            report.update(status='FAILED', error=str(error))
            report_path.write_text(json.dumps(report, indent=2) + '\n')
            raise

    def _validate_entry(self, entry: dict) -> None:
        """Reject discovery failures and require a full pinned upstream commit."""
        for field in ('discovery_error', 'upstream_resolution_error',
                      'upstream_dependency_resolution_error'):
            if entry.get(field):
                raise ValueError(entry[field])

        if not re.fullmatch(r'[a-f0-9]{40}', entry.get('upstream_sha', '')):
            raise ValueError('Source preparation requires a plan-pinned upstream_sha')

    def _packaging_source(self, entry: dict) -> tuple[Path, Path | None, dict]:
        """Choose pinned Git packaging or an explicitly permitted published fallback."""
        baseline_dsc = None
        if entry.get('packaging_source_kind') == 'git':
            try:
                packaging_tree, packaging_source = checkout_packaging_tree(self, entry)
            except PackagingIdentityMismatch as error:
                if not _can_use_published_source_fallback(entry):
                    raise

                rejected = {
                    'kind': 'git',
                    'role': entry.get('packaging_source_role'),
                    'repository': entry.get('packaging_source_repository'),
                    'branch': entry.get('packaging_branch'),
                    'sha': entry.get('packaging_sha'),
                    'reason': str(error),
                }
                shutil.rmtree(self.work / 'packaging')
                packaging_tree, baseline_dsc, packaging_source = extract_archive_packaging(
                    self, entry)
                packaging_source['rejected_git_source'] = rejected
        else:
            packaging_tree, baseline_dsc, packaging_source = extract_archive_packaging(self, entry)

        return packaging_tree, baseline_dsc, packaging_source

    def _select_upstream(self, entry: dict, cutoff: str | None) -> tuple[Path, dict]:
        """Materialize the pinned upstream commit and verify selection preserves its identity."""
        checkout = self.work / 'upstream-discovery'
        self.command('git', 'clone', '--no-checkout', entry['upstream_repository'], str(checkout))
        # Stable branch is chosen in the catalog from release declarations. A
        # deleted/missing declared branch is an error, never a master fallback.
        self.command('git', 'cat-file', '-e', entry['upstream_sha'] + '^{commit}', cwd=checkout)
        self.command('git', 'update-ref', 'refs/remotes/origin/packagetest-frozen', entry['upstream_sha'], cwd=checkout)
        selected = select_commit(self, checkout, 'packagetest-frozen', None)
        selected.update(ref=entry['upstream_ref'], cutoff=cutoff)
        if selected['sha'] != entry['upstream_sha']:
            raise ValueError('Source preparation changed the plan-pinned upstream SHA')

        return checkout, selected

    def _snapshot_archive(self, entry: dict, checkout: Path, selected: dict, orig: Path, source: str, upstream: str) -> dict:
        """Generate the configured snapshot from the verified upstream selection."""
        if entry.get('snapshot_backend') == 'git-archive':
            snapshot = git_archive(self, checkout, selected, orig, source)
        else:
            snapshot_spec = {**selected, 'repository': entry['upstream_repository'],
                             'build_requirements': BUILD_REQUIREMENTS + (SCM_REQUIREMENTS if entry.get('snapshot_version_backend') == 'setuptools-scm' else []),
                             'version_backend': entry.get('snapshot_version_backend', 'pbr'), 'archive_format': 'portable-v1'}
            snapshot = build_snapshot(self, {'source': source, 'input': {'snapshot': snapshot_spec, 'upstream_version': upstream}}, orig)

        return snapshot

    def _preserve_components(self, entry: dict, baseline_dsc: Path | None, source: str, upstream: str, tree: Path, report: dict) -> None:
        """Preserve supplementary archives from the selected packaging source."""
        if baseline_dsc is not None:
            report['supplementary_orig_components'] = preserve_orig_components(
                baseline_dsc, source, upstream, tree, self.root)
        else:
            report['supplementary_orig_components'] = preserve_catalog_orig_components(
                self, entry, upstream, tree, self.root)

    def _copy_packaging(self, packaging_tree: Path, tree: Path) -> Path:
        """Install Debian packaging and retain its pre-adaptation proposal baseline."""
        if (tree / 'debian').exists():
            raise ValueError('Upstream snapshot unexpectedly contains Debian packaging')

        shutil.copytree(packaging_tree / 'debian', tree / 'debian', symlinks=True)
        ubuntu_maintainer(tree / 'debian' / 'control')
        proposal_baseline = self.work / 'packaging-proposal-baseline'
        shutil.copytree(tree / 'debian', proposal_baseline, symlinks=True)
        return proposal_baseline

    def _apply_remediation(self, remediation_patch: Path | None, tree: Path, report: dict) -> None:
        """Apply only a validated source repair and retain its patch identity."""
        if remediation_patch is not None:
            from .failure_analysis import validate_source_patch
            patch = remediation_patch.read_text()
            validation = validate_source_patch(patch, tree, apply=True)
            if validation['result'] != 'APPLIED':
                raise ValueError('Remediation patch rejected: ' + validation['error'])

            report['remediation'] = {
                'patch_file': remediation_patch.name,
                'patch_sha256': validation['patch_sha256'],
                'paths': validation['paths'],
            }
            report['packaging_adjustments'].append({
                'action': 'local-ai-remediation',
                'patch_file': remediation_patch.name,
                'patch_sha256': validation['patch_sha256'],
                'paths': validation['paths'],
            })

    def _build_source_package(self, tree: Path, source: str, version: str, distribution: str, openstack_series: str, selected: dict) -> list[Path]:
        """Build one source package and validate its identity and extractability."""
        self.command('dch', '--newversion', version, '--distribution', distribution,
                      '--force-distribution',
                      f'Nightly OpenStack {openstack_series} snapshot from pinned upstream commit '
                      + selected['sha'] + '.',
                      cwd=tree, env={'DEBFULLNAME': 'Packaging Build Agent', 'DEBEMAIL': 'packaging-agent@example.invalid'})
        self.command('dpkg-buildpackage', '-S', '-d', '-nc', '-us', '-uc', cwd=tree,
                      env={'SOURCE_DATE_EPOCH': selected['commit_timestamp']})
        dscs = list(self.root.glob('*.dsc'))
        if len(dscs) != 1:
            raise ValueError('Expected exactly one generated Debian source package')

        verify_source(dscs[0], source, version)
        validation_tree = self.work / 'source-package-validation'
        self.command('dpkg-source', '-x', str(dscs[0]), str(validation_tree))
        return dscs


def download_verified(url: str, destination: Path, digest: str,
                      algorithm: str = 'sha256') -> None:
    if urlparse(url).scheme != 'https':
        raise ValueError('Pinned source downloads require HTTPS')
    with urlopen(url, timeout=120) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)
    if algorithm not in {'sha256', 'sha512'}:
        raise ValueError(f'Unsupported source checksum: {algorithm}')
    with destination.open('rb') as stream:
        actual = hashlib.file_digest(stream, algorithm).hexdigest()
    if actual != digest:
        raise ValueError(f'Source checksum mismatch: {destination.name}')


def source_digest(record: dict) -> tuple[str, str]:
    for algorithm in ('sha256', 'sha512'):
        if record.get(algorithm):
            return algorithm, record[algorithm]
    raise ValueError('Pinned source record has no strong checksum')


def snapshot_debian_version(archive_version: str, upstream: str,
                            revision_suffix: str = '-0ubuntu1~hibiscus1') -> str:
    epoch = archive_version.split(':', 1)[0] + ':' if ':' in archive_version else ''
    if not re.fullmatch(r'-[0-9A-Za-z.+~]+', revision_suffix):
        raise ValueError('Invalid snapshot Debian revision suffix')
    return epoch + upstream + revision_suffix



def ubuntu_maintainer(control: Path) -> None:
    """Record the original Debian maintainer when creating an Ubuntu revision."""
    text = control.read_text()
    source, separator, binaries = text.partition('\n\n')
    match = re.search(r'^Maintainer: (.*(?:\n[ \t].*)*)', source, re.M)
    if not match:
        raise ValueError('Source control lacks Maintainer')
    original = match.group(1)
    # Ubuntu's canonical maintainer address is hosted on lists.ubuntu.com;
    # individual Ubuntu maintainers commonly use ubuntu.com directly.
    if re.search(r'@(?:[A-Za-z0-9-]+\.)*ubuntu\.com\b', original, re.I):
        return
    replacement = 'Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>'
    if not re.search(r'^XSBC-Original-Maintainer:', source, re.M):
        replacement += '\nXSBC-Original-Maintainer: ' + original
    source = source[:match.start()] + replacement + source[match.end():]
    control.write_text(source + separator + binaries)


def _add_control_dependency(text: str, field: str, package: str) -> str:
    """Insert one unversioned dependency into a multiline control field."""
    lines = text.splitlines(keepends=True)
    start, end = _control_field_bounds(lines, field)
    current = ''.join(lines[start:end])
    if re.search(rf'(?<![A-Za-z0-9+.-]){re.escape(package)}(?![A-Za-z0-9+.-])', current):
        return text

    insert = _dependency_insert_position(lines, start, end, package)
    lines.insert(insert, f' {package},\n')
    return ''.join(lines)


def upstream_dependency_adjustments(entry: dict, tree: Path) -> list[dict]:
    """Add missing, archive-proven upstream dependencies to the build candidate.

    Test and build-system dependencies are confined to the source build
    dependency field. Runtime metadata is retained in the plan for review but
    is not copied automatically into binary Depends because Debian packaging
    can intentionally split optional runtime features. Every mutation becomes
    part of the downloadable packaging proposal and remains subject to the
    normal rebuild and autopkgtest gates.
    """
    records = entry.get('upstream_dependency_requirements', [])
    usable = _usable_dependency_records(records)
    if not usable:
        return []

    path = tree / 'debian' / 'control'
    before = path.read_text()
    paragraphs = re.split(r'(\n\s*\n)', before)
    source = paragraphs[0]
    editor = BuildDependencyEditor(source)
    actions = editor.adapt(_group_archive_requirements(usable))
    paragraphs[0] = editor.source
    revised = ''.join(paragraphs)
    if revised != before:
        path.write_text(revised)

    return actions


def _add_quilt_patch(tree: Path, name: str, content: str) -> None:
    """Create a uniquely named packaging patch and append it to the quilt series."""
    patches = tree / 'debian' / 'patches'
    patches.mkdir(parents=True, exist_ok=True)
    patch = patches / name
    if patch.exists() or patch.is_symlink():
        raise ValueError(f'Packaging already contains generated patch name: {name}')

    patch.write_text(content)
    series = patches / 'series'
    _append_quilt_series(series, name)


def _setuptools_configuration(text: str) -> tuple[list, list] | None:
    """Read discovery configuration while treating invalid TOML as non-actionable."""
    try:
        setuptools = tomllib.loads(text).get('tool', {}).get('setuptools', {})
    except tomllib.TOMLDecodeError:
        return None

    packages = setuptools.get('packages', {}) if isinstance(setuptools, dict) else {}
    return _configured_packages(packages)


def _configured_packages(packages: dict | list) -> tuple[list, list]:
    """Separate find patterns from explicitly listed setuptools packages."""
    includes = packages.get('find', {}).get('include', []) if isinstance(packages, dict) else []
    explicit = packages if isinstance(packages, list) else []
    return includes if isinstance(includes, list) else [], explicit


def _package_directory(tree: Path, include: object) -> tuple[str, Path] | None:
    """Resolve supported literal package patterns to existing source directories."""
    if not isinstance(include, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*\*?', include):
        return None

    name = include.removesuffix('*')
    path = tree.joinpath(*name.split('.'))
    if not path.is_dir():
        return None

    return name, path


def _importable_subdirectories(root: Path) -> list[Path]:
    """Find traditional Python subpackages beneath an existing package root."""
    return [child for child in root.rglob('*')
            if child.is_dir() and (child / '__init__.py').is_file()]


def _runtime_package_file(path: Path) -> bool:
    """Identify visible non-Python files eligible for package-data inclusion."""
    return (path.is_file() and not path.name.startswith('.')
            and path.suffix not in {'.py', '.pyi', '.pyc'} and path.name != 'py.typed')


def _runtime_package_files(directory: Path) -> list[str]:
    """List eligible runtime data in a stable order for generated TOML."""
    return sorted(child.name for child in directory.iterdir() if _runtime_package_file(child))


@dataclass
class SetuptoolsDiscovery:
    """Accumulate missing subpackages and runtime data without modifying source."""

    tree: Path
    explicit: list
    replacements: dict[str, str] = dataclass_field(default_factory=dict)
    explicit_additions: set[str] = dataclass_field(default_factory=set)
    omitted: dict[str, list[str]] = dataclass_field(default_factory=dict)
    package_roots: list[tuple[str, Path]] = dataclass_field(default_factory=list)
    package_data: dict[str, list[str]] = dataclass_field(default_factory=dict)

    def inspect(self, configured: list, text: str) -> None:
        """Scan configured roots and infer data only when no data table is present."""
        for include in configured:
            self.inspect_root(include)

        if '[tool.setuptools.package-data]' not in text:
            self.inspect_data()

    def inspect_root(self, include: object) -> None:
        """Record the root and any importable packages excluded by its configuration."""
        resolved = _package_directory(self.tree, include)
        if resolved is None:
            return

        name, root = resolved
        self.package_roots.append((name, root))
        subpackages = sorted(child.relative_to(self.tree).as_posix().replace('/', '.')
                             for child in _importable_subdirectories(root))
        if subpackages:
            self.include_subpackages(include, subpackages)

    def include_subpackages(self, include: str, subpackages: list[str]) -> None:
        """Preserve explicit lists and widen only exact find patterns."""
        if include in self.explicit:
            self.explicit_additions.update(set(subpackages) - set(self.explicit))
        elif not include.endswith('*'):
            self.replacements[include] = include + '*'

        self.omitted[include] = subpackages

    def inspect_data(self) -> None:
        """Collect runtime files by their owning importable package."""
        for root_name, root in self.package_roots:
            for directory in [root, *_importable_subdirectories(root)]:
                files = _runtime_package_files(directory)
                if files:
                    relative = directory.relative_to(root).parts
                    self.package_data['.'.join((root_name, *relative))] = files

    def has_changes(self) -> bool:
        """Report whether discovery found an actionable metadata omission."""
        return bool(self.replacements or self.explicit_additions or self.package_data)

    def rewrite(self, text: str) -> str:
        """Render discovery changes while preserving TOML structure and comments."""
        revised = _widen_package_patterns(text, self.replacements)
        if self.explicit_additions:
            revised = _expand_explicit_packages(revised, self.explicit_additions)

        if self.package_data:
            revised = _append_package_data(revised, self.package_data)

        return revised

    def record(self, name: str) -> dict:
        """Expose the exact proposed metadata changes for human review."""
        return {
            'action': 'add-quilt-patch', 'name': name,
            'reason': 'Setuptools metadata omits importable subpackages or runtime package data.',
            'replacements': self.replacements, 'omitted_subpackages': self.omitted,
            'explicit_package_additions': sorted(self.explicit_additions),
            'package_data': self.package_data, 'human_review_required': True,
        }


def _widen_package_patterns(text: str, replacements: dict[str, str]) -> str:
    """Widen exact single-line find patterns only when their location is unambiguous."""
    for old, new in replacements.items():
        pattern = rf'(?m)^(\s*include\s*=\s*\[[^\n]*)(["\']){re.escape(old)}\2([^\n]*\]\s*)$'
        text, count = re.subn(
            pattern, lambda match: match.group(1) + match.group(2) + new
            + match.group(2) + match.group(3), text, count=1)
        if count != 1:
            raise ValueError(f'Cannot safely widen setuptools package discovery for {old}')

    return text


def _expand_explicit_packages(text: str, additions: set[str]) -> str:
    """Append missing explicit packages through the comment-preserving TOML parser."""
    try:
        document = tomlkit.parse(text)
        packages = document['tool']['setuptools']['packages']
        if not isinstance(packages, tomlkit.items.Array):
            raise TypeError('tool.setuptools.packages is not an array')

        for package in sorted(additions):
            packages.append(package)

        return tomlkit.dumps(document)
    except (TOMLKitParseError, KeyError, TypeError) as error:
        raise ValueError('Cannot safely expand explicit setuptools package discovery') from error


def _append_package_data(text: str, package_data: dict[str, list[str]]) -> str:
    """Append a deterministically ordered package-data table without altering existing text."""
    if not text.endswith('\n'):
        text += '\n'

    text += '\n[tool.setuptools.package-data]\n'
    for package, files in sorted(package_data.items()):
        text += f'{json.dumps(package)} = {json.dumps(files)}\n'

    return text


def _publish_setuptools_patch(patch_tree: Path, original: str, revised: str) -> str:
    """Add a reviewable quilt patch rather than modifying upstream configuration directly."""
    diff = ''.join(difflib.unified_diff(
        original.splitlines(keepends=True), revised.splitlines(keepends=True),
        fromfile='a/pyproject.toml', tofile='b/pyproject.toml', n=3))
    name = 'complete-setuptools-packaging.patch'
    header = (
        'Description: Complete setuptools package discovery and runtime data\n'
        ' The upstream snapshot omits importable subpackages or package data from\n'
        ' its setuptools configuration. Include them in builds and tests.\n'
        'Forwarded: no\n'
        'Author: Packaging Build Agent <packaging-agent@example.invalid>\n'
    )
    _add_quilt_patch(patch_tree, name, header + diff)
    return name


def _setuptools_discovery_adjustments(tree: Path, patch_tree: Path) -> list[dict]:
    """Propose missing setuptools packages and data based on the supplied source state."""
    path = tree / 'pyproject.toml'
    if not path.is_file():
        return []

    text = path.read_text()
    configuration = _setuptools_configuration(text)
    if configuration is None:
        return []

    includes, explicit = configuration
    discovery = SetuptoolsDiscovery(tree, explicit)
    discovery.inspect([*includes, *explicit], text)
    if not discovery.has_changes():
        return []

    name = _publish_setuptools_patch(patch_tree, text, discovery.rewrite(text))
    return [discovery.record(name)]


def setuptools_discovery_adjustments(tree: Path) -> list[dict]:
    """Patch setuptools metadata against the state produced by active patches.

    Generated patches are appended to ``debian/patches/series``. Their diff
    therefore has to use the source state after every preceding quilt patch,
    rather than the pristine upstream snapshot.
    """
    series = tree / 'debian' / 'patches' / 'series'
    active = _active_quilt_lines(series)
    if not active:
        return _setuptools_discovery_adjustments(tree, tree)

    with tempfile.TemporaryDirectory(prefix='packagetest-post-quilt-') as directory:
        patched = Path(directory) / 'source'
        shutil.copytree(tree, patched, symlinks=True)
        environment = os.environ.copy()
        environment['QUILT_PATCHES'] = 'debian/patches'
        result = subprocess.run(
            ['quilt', '--quiltrc', '/dev/null', 'push', '-a'], cwd=patched,
            env=environment, text=True, capture_output=True)
        if result.returncode != 0:
            # Source preparation will diagnose and remediate the actual failed
            # packaging patch. Do not compound it with a generated patch based
            # on a source state that can never exist.
            return []

        return _setuptools_discovery_adjustments(patched, tree)


def autopkgtest_packaging_adjustments(entry: dict, tree: Path) -> list[dict]:
    """Adapt generated and superficial Python tests to the snapshot metadata."""
    actions = []
    tests = tree / 'debian' / 'tests'
    control = tests / 'control'
    actions.extend(_version_probe_adjustments(control))
    actions.extend(_autodep8_import_adjustments(entry, tree, tests))
    return actions


def write_packaging_proposal(entry: dict, baseline: Path, tree: Path, output: Path,
                             actions: list[dict]) -> dict | None:
    """Export temporary packaging adaptations for human review upstream.

    The patch is rooted at ``debian/`` so it can be reviewed against the pinned
    packaging repository. Choosing the final submission target remains a human
    review decision.
    """
    if not actions:
        return None

    repositories, branches = _proposal_destinations(entry)
    output.mkdir(parents=True, exist_ok=True)
    patch_path = output / 'packaging-proposal.patch'
    with tempfile.TemporaryDirectory(prefix='packagetest-proposal-') as temporary:
        repository = Path(temporary) / 'repository'
        repository.mkdir()
        shutil.copytree(baseline, repository / 'debian', symlinks=True)
        environment = {**os.environ,
                       'GIT_AUTHOR_NAME': 'Packaging Build Agent',
                       'GIT_AUTHOR_EMAIL': 'packaging-agent@example.invalid',
                       'GIT_COMMITTER_NAME': 'Packaging Build Agent',
                       'GIT_COMMITTER_EMAIL': 'packaging-agent@example.invalid',
                       'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+00:00',
                       'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+00:00'}
        subprocess.run(['git', 'init', '--quiet'], cwd=repository, env=environment, check=True)
        subprocess.run(['git', 'add', '--all'], cwd=repository, env=environment, check=True)
        subprocess.run(['git', 'commit', '--quiet', '--no-gpg-sign', '-m', 'archive packaging baseline'],
                       cwd=repository, env=environment, check=True)
        shutil.rmtree(repository / 'debian')
        shutil.copytree(tree / 'debian', repository / 'debian', symlinks=True)
        subprocess.run(['git', 'add', '--all'], cwd=repository, env=environment, check=True)
        result = subprocess.run(['git', 'diff', '--cached', '--binary', '--full-index', 'HEAD', '--', 'debian'],
                                cwd=repository, env=environment, text=True, capture_output=True, check=True)
        if not result.stdout:
            return None

        patch_path.write_text(result.stdout)

    archive_algorithm, archive_digest = source_digest(entry['archive_source'])
    proposal = {
        'schema_version': 1,
        'source': entry['source'],
        'status': 'candidate',
        'human_review_required': True,
        'selected_target': None,
        'destination_candidates': repositories,
        'branch_candidates': branches,
        'packaging_base': {
            'kind': entry.get('packaging_source_kind', 'archive'),
            'role': entry.get('packaging_source_role'),
            'repository': entry.get('packaging_source_repository'),
            'branch': entry.get('packaging_branch'),
            'sha': entry.get('packaging_sha'),
        },
        'archive_base': {
            'version': entry['archive_version'],
            'url': entry['archive_source']['url'],
            archive_algorithm: archive_digest,
            'generic_normalization_before_diff': ['ubuntu-maintainer'],
        },
        'patch': {'file': patch_path.name, 'sha256': sha256(patch_path)},
        'actions': actions,
        'removal_condition': 'Retire after the reviewed change is accepted in the selected packaging branch.',
    }
    (output / 'packaging-proposal.json').write_text(json.dumps(proposal, indent=2) + '\n')
    return proposal


def extract_snapshot(archive: Path, destination: Path) -> None:
    """Safely extract one snapshot root and flatten it into the requested directory."""
    destination.mkdir()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        root_name = _snapshot_root_name(members)
        _validate_snapshot_paths(members)
        # Python data filtering prevents symlinks escaping the extraction root.
        source.extractall(destination, filter='data')

    root = destination / root_name
    if not root.is_dir():
        raise ValueError('Snapshot archive root is not a directory')

    for child in root.iterdir():
        child.rename(destination / child.name)

    root.rmdir()


def preserve_orig_components(baseline_dsc: Path, source: str, upstream: str,
                             tree: Path, output: Path) -> list[dict]:
    """Carry checksum-pinned supplementary orig tarballs into the new source.

    Debian packaging can depend on components absent from upstream Git, such
    as Horizon's separately archived XStatic assets. Preserve their bytes and
    component layout while naming them for the new snapshot version.
    """
    components = []
    for digest, size, filename in checksum_entries(fields(baseline_dsc)):
        if '.orig-' not in filename or filename.endswith('.asc'):
            continue

        component, compression = _orig_component_name(source, filename)
        archive = baseline_dsc.parent / filename
        _verify_component_bytes(archive, size, digest, filename)
        destination = output / f'{source}_{upstream}.orig-{component}.tar.{compression}'
        component_tree = tree / component
        _install_orig_component(archive, destination, component_tree, component)
        components.append({'component': component, 'archive_file': filename,
                           'snapshot_file': destination.name, 'sha256': digest})

    return components


def preserve_catalog_orig_components(build: SourcePreparer, entry: dict, upstream: str,
                                     tree: Path, output: Path) -> list[dict]:
    """Restore supplementary orig components while packaging comes from Git."""
    source = entry['source']
    components = []
    directory = build.work / 'supplementary-orig'
    for record in entry['archive_source'].get('files', []):
        filename = record['name']
        if '.orig-' not in filename or filename.endswith('.asc'):
            continue

        component, compression = _orig_component_name(source, filename)
        directory.mkdir(exist_ok=True)
        archive = directory / filename
        algorithm, digest = source_digest(record)
        download_verified(record['url'], archive, digest, algorithm)
        if archive.stat().st_size != record['size']:
            raise ValueError('Supplementary orig size mismatch: ' + filename)

        destination = output / f'{source}_{upstream}.orig-{component}.tar.{compression}'
        component_tree = tree / component
        _install_orig_component(archive, destination, component_tree, component)
        components.append({'component': component, 'archive_file': filename,
                           'snapshot_file': destination.name,
                           algorithm: digest})

    return components


def checkout_packaging_tree(build: SourcePreparer, entry: dict) -> tuple[Path, dict]:
    """Checkout the exact source-package Git commit pinned by the run plan."""
    if entry.get('packaging_source_kind') != 'git':
        raise ValueError('Catalog entry does not select Git packaging')

    repository, revision, branch = _packaging_coordinates(entry)
    checkout = build.work / 'packaging'
    build.command('git', 'init', '--quiet', str(checkout))
    build.command('git', 'remote', 'add', 'origin', repository, cwd=checkout)
    PackagingFetcher(build, checkout, repository, revision).fetch()
    build.command('git', 'checkout', '--detach', revision, cwd=checkout)
    control = _verify_packaging_checkout(build, checkout, entry, revision)
    _verify_packaging_role(build, checkout, control, entry)
    return checkout, {
        'kind': 'git', 'role': entry.get('packaging_source_role'),
        'repository': repository, 'branch': branch, 'sha': revision,
        'upstream_branch': entry.get('packaging_upstream_branch'),
        'upstream_sha': entry.get('packaging_upstream_sha'),
        'pristine_tar_sha': entry.get('packaging_pristine_tar_sha'),
    }


def extract_archive_packaging(build: SourcePreparer, entry: dict) -> tuple[Path, Path, dict]:
    """Extract published packaging only when the run lock records a Git fallback."""
    source = entry['source']
    baseline_dir = build.work / 'baseline'
    baseline_dir.mkdir()
    spec = entry['archive_source']
    baseline_dsc = baseline_dir / Path(urlparse(spec['url']).path).name
    algorithm, source_checksum = source_digest(spec)
    download_verified(spec['url'], baseline_dsc, source_checksum, algorithm)
    for digest, _, filename in checksum_entries(fields(baseline_dsc)):
        download_verified(urljoin(spec['url'], filename), baseline_dir / filename, digest)
    verify_source(baseline_dsc, source, entry['archive_version'])
    packaging_tree = build.work / 'packaging'
    build.command('dpkg-source', '--skip-patches', '-x', str(baseline_dsc), str(packaging_tree))
    return packaging_tree, baseline_dsc, {
        'kind': 'archive',
        'role': ('uca-ppa-source' if _is_uca_ppa_source(entry) else 'published-source'),
        'url': spec['url'], algorithm: source_checksum,
        'reason': entry.get('packaging_resolution_error'),
    }


def git_archive(build: SourcePreparer, checkout: Path, selected: dict, destination: Path, package: str) -> dict:
    raw = build.work / 'git-source.tar'
    build.command('git', 'archive', '--format=tar', f'--prefix={package}-{selected["upstream_version"]}/',
                  f'--output={raw}', selected['sha'], cwd=checkout)
    epoch = int(selected['commit_timestamp'])
    with tarfile.open(raw) as source, destination.open('wb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target:
                for member in sorted(source.getmembers(), key=lambda item: item.name):
                    member.uid = member.gid = 0
                    member.uname = member.gname = ''
                    member.mtime = epoch
                    member.pax_headers = {}
                    contents = source.extractfile(member).read() if member.isfile() else None
                    target.addfile(member, io.BytesIO(contents) if contents is not None else None)
    return {**selected, 'sdist_sha256': sha256(destination), 'sdist_file': destination.name, 'backend': 'git-archive'}




def prepare_source(entry: dict, destination: Path, *, cutoff: str | None = None,
                   remediation_patch: Path | None = None,
                   target: dict | None = None) -> dict:
    """Prepare a source in a new workspace; existing generations are never overwritten."""
    return SourcePreparer(destination).prepare(entry, cutoff=cutoff,
                                               remediation_patch=remediation_patch, target=target)


def _snapshot_root_name(members: list[tarfile.TarInfo]) -> str:
    """Require exactly one nonempty top-level path in the snapshot archive."""
    roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
    if len(roots) != 1:
        raise ValueError('Snapshot archive must contain exactly one top-level directory')

    return next(iter(roots))


def _validate_snapshot_paths(members: list[tarfile.TarInfo]) -> None:
    """Reject absolute and parent-relative names before filtered extraction."""
    for member in members:
        name = Path(member.name)
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe snapshot archive path')


def _orig_component_name(source: str, filename: str) -> tuple[str, str]:
    """Parse a supplementary orig name without accepting paths or unsupported compression."""
    match = re.fullmatch(re.escape(source) + r'_[^/]+\.orig-([A-Za-z0-9-]+)\.tar\.(gz|xz|bz2|lzma)', filename)
    if not match:
        raise ValueError('Unsupported supplementary orig filename: ' + filename)

    component, compression = match.groups()
    return component, compression


def _verify_component_bytes(archive: Path, size: int, digest: str, filename: str) -> None:
    """Verify supplementary source bytes before extracting or renaming them."""
    if archive.stat().st_size != int(size) or sha256(archive) != digest:
        raise ValueError('Supplementary orig checksum mismatch: ' + filename)


def _install_orig_component(archive: Path, destination: Path, component_tree: Path, component: str) -> None:
    """Reject file or directory collisions before preserving a supplementary component."""
    if destination.exists() or destination.is_symlink() or component_tree.exists() or component_tree.is_symlink():
        raise ValueError('Supplementary orig component collides with snapshot: ' + component)

    extract_snapshot(archive, component_tree)
    shutil.copyfile(archive, destination)


def _packaging_coordinates(entry: dict) -> tuple[str, str, str]:
    """Require repository, branch, and complete commit coordinates for Git packaging."""
    repository = entry.get('packaging_source_repository')
    revision = entry.get('packaging_sha')
    branch = entry.get('packaging_branch')
    if not repository or not re.fullmatch(r'[a-f0-9]{40}', revision or '') or not branch:
        raise ValueError('Git packaging requires a pinned repository, branch, and commit')

    return repository, revision, branch


class PackagingFetcher:
    """Own bounded Git fetch retries and Launchpad-specific backoff for one checkout."""

    def __init__(self, build: SourcePreparer, checkout: Path, repository: str, revision: str) -> None:
        """Select retry policy without changing the pinned revision being requested."""
        self.build = build
        self.checkout = checkout
        self.revision = revision
        self.launchpad = _is_launchpad_git(repository)
        self.attempts = LAUNCHPAD_FETCH_ATTEMPTS if self.launchpad else 3

    def fetch(self) -> None:
        """Retry transient failures, stopping immediately on permanent Launchpad errors."""
        fetch_error = None
        for attempt in range(1, self.attempts + 1):
            try:
                self.build.command('git', '-c', 'http.lowSpeedLimit=1000',
                                   '-c', 'http.lowSpeedTime=60', 'fetch', '--no-tags',
                                   '--depth=1', 'origin', self.revision, cwd=self.checkout)
                return
            except RuntimeError as exc:
                fetch_error = exc
                if self._permanent(exc):
                    break

                self._pause(attempt, exc)

        if fetch_error is not None:
            raise fetch_error

    def _permanent(self, error: Exception) -> bool:
        """Limit permanent-failure recognition to the Launchpad fetch policy."""
        return self.launchpad and _is_permanent_launchpad_fetch_error(error)

    def _pause(self, attempt: int, error: Exception) -> None:
        """Back off only when another attempt remains, jittering shared Launchpad retries."""
        if attempt >= self.attempts:
            return

        if self.launchpad:
            delay = LAUNCHPAD_FETCH_BACKOFF[attempt - 1] + random.uniform(0, 5)
            label = 'Launchpad Git'
        else:
            delay = attempt * 2
            label = 'Git'

        print(f'{label} fetch attempt {attempt}/{self.attempts} failed; '
              f'retrying in {delay:.1f}s: {error}', flush=True)
        time.sleep(delay)


def _verify_packaging_checkout(build: SourcePreparer, checkout: Path, entry: dict, revision: str) -> Path:
    """Verify the checked-out commit and source identity in control and changelog."""
    actual = build.command('git', 'rev-parse', 'HEAD', cwd=checkout)
    if actual != revision:
        raise ValueError('Packaging checkout differs from the plan-pinned commit')

    control = checkout / 'debian' / 'control'
    if not control.is_file() or fields(control).get('Source') != entry['source']:
        raise ValueError('Launchpad packaging debian/control source mismatch')

    changelog_source = build.command('dpkg-parsechangelog', '-S', 'Source', cwd=checkout)
    if changelog_source != entry['source']:
        raise ValueError('Launchpad packaging debian/changelog source mismatch')

    return control


def _verify_packaging_role(build: SourcePreparer, checkout: Path, control: Path, entry: dict) -> None:
    """Enforce importer version identity and archive binary-set compatibility."""
    role = entry.get('packaging_source_role')
    if role == 'ubuntu-importer':
        _verify_importer_identity(build, checkout, control, entry)
    elif role in {'series-archive-vcs', 'archive-vcs'}:
        binaries = sorted(re.findall(r'^Package:\s*(\S+)', control.read_text(), re.M))
        expected = sorted(entry['binaries'])
        if binaries != expected:
            raise PackagingIdentityMismatch(
                f'Generic archive VCS binary set differs from published archive: '
                f'{binaries} != {expected}')


def _verify_importer_identity(build: SourcePreparer, checkout: Path, control: Path, entry: dict) -> None:
    """Require Ubuntu importer packaging to match the exact published version and binaries."""
    version = build.command('dpkg-parsechangelog', '-S', 'Version', cwd=checkout)
    if version != entry['archive_version']:
        raise PackagingIdentityMismatch(
            f'Ubuntu importer packaging version {version} differs from archive '
            f'{entry["archive_version"]}')

    binaries = sorted(re.findall(r'^Package:\s*(\S+)', control.read_text(), re.M))
    if binaries != sorted(entry['binaries']):
        raise PackagingIdentityMismatch(
            f'Ubuntu importer binary set differs from archive: {binaries} != '
            f'{sorted(entry["binaries"])}')


def _proposal_destinations(entry: dict) -> tuple[list[dict], list[str]]:
    """Require explicit candidate repositories and branches for reviewable packaging proposals."""
    repositories = _candidate_repositories(entry)
    if not repositories:
        raise ValueError('Packaging proposal requires a destination repository candidate')

    branches = entry.get('packaging_branch_candidates') or []
    if not branches:
        raise ValueError('Packaging proposal requires destination branch candidates')

    return repositories, branches


def _candidate_repositories(entry: dict) -> list[dict]:
    """Preserve origin preference while deduplicating repository URLs."""
    repositories = []
    for role, key in [('ubuntu-openstack', 'packaging_repository'),
                      ('ubuntu-importer', 'ubuntu_importer_repository'),
                      ('archive', 'archive_packaging_repository')]:
        repository = entry.get(key)
        if repository and repository not in {item['repository'] for item in repositories}:
            repositories.append({'role': role, 'repository': repository})

    return repositories


def _append_quilt_series(series: Path, name: str) -> None:
    """Append a patch name while preserving a missing final newline in existing series."""
    existing = series.read_text() if series.is_file() else ''
    if existing and not existing.endswith('\n'):
        existing += '\n'

    series.write_text(existing + name + '\n')


def _active_quilt_lines(series: Path) -> list[str]:
    """Read nonempty uncommented entries before constructing the post-quilt source state."""
    active = [line for line in series.read_text().splitlines()
              if line.strip() and not line.lstrip().startswith('#')] \
        if series.is_file() else []
    return active


def _setup_entry_points(setup_cfg: str | None) -> list[dict]:
    """Read entry points from setup.cfg without importing upstream code."""
    result = []
    if setup_cfg:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(setup_cfg)
        except configparser.Error as exc:
            raise ValueError(f'Invalid setup.cfg while inspecting entry points: {exc}') from exc

        if parser.has_section('entry_points'):
            for group, value in parser.items('entry_points'):
                result.extend(_setup_group_entries(group, value))

    return result


def _project_entry_points(pyproject: str | None) -> list[dict]:
    """Read PEP 621 entry points without importing upstream code."""
    result = []
    if pyproject:
        try:
            project = tomllib.loads(pyproject).get('project', {})
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f'Invalid pyproject.toml while inspecting entry points: {exc}') from exc

        groups = {
            'console_scripts': project.get('scripts', {}),
            'gui_scripts': project.get('gui-scripts', {}),
            **project.get('entry-points', {}),
        }
        for group, declarations in groups.items():
            if not isinstance(declarations, dict):
                continue

            result.extend(_project_group_entries(group, declarations))

    return result


def _setup_group_entries(group: str, value: str) -> list[dict]:
    """Parse named declarations while ignoring comments and incomplete lines."""
    result = []
    for line in value.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            name, target = _entry_point_pair(line)
            result.append({'group': group.replace('-', '_'), 'name': name,
                           'target': target, 'declared_in': 'setup.cfg'})

    return result


def _entry_point_pair(line: str) -> tuple[str, str]:
    """Split the first assignment and normalize surrounding whitespace."""
    name, target = line.split('=', 1)
    return name.strip(), target.strip()


def _matching_archive_tags(checkout: Path, selected: dict, archive_upstream: str) -> list[str]:
    """Find merged tags matching the archive upstream version exactly."""
    from .versioning import upstream_version_to_debian_version
    tags = subprocess.run(
        ['git', '-C', str(checkout), 'tag', '--merged', selected['sha']],
        text=True, capture_output=True, check=True).stdout.splitlines()
    matches = []
    for tag in tags:
        normalized = upstream_version_to_debian_version(tag).rsplit('-', 1)[0]
        if normalized == archive_upstream:
            matches.append(tag)

    return matches


def _evolution_baseline_lines(delta: dict, lines: list[str]) -> None:
    """Describe the comparison baseline and qualify unmatched archive versions."""
    if delta['basis'] == 'no-target-archive-baseline':
        lines.append('No official package exists in the target archive, so an official '
                     'commit delta cannot be calculated yet.')
    else:
        lines.append(f"{delta['count']} commits since `{delta['comparison_tag']}` "
                     f"({delta['basis'].replace('-', ' ')}).")

    if (delta['basis'] != 'no-target-archive-baseline'
            and not delta['comparison_tag_matches_archive_version']):
        lines.extend(['', f"Archive upstream version `{delta['archive_upstream_version']}` did not "
                      'match that tag; verify the official-package baseline.'])


def _evolution_entry_lines(report: dict, lines: list[str]) -> None:
    """Append entry-point review findings or the explicit empty result."""
    introduced = report['introduced_entry_points']
    if introduced:
        lines.extend(['', '### New commands, services, and entry points', '',
                      'Human review is required to decide whether any item needs a new binary package.', '',
                      '| Group | Name | Packaging |', '|---|---|---|'])
        lines.extend(f"| `{item['group']}` | `{item['name']}` | {item['packaging']} |"
                     for item in introduced)
    else:
        lines.extend(['', 'No new statically declared entry points were detected.'])


def _control_field_bounds(lines: list[str], field: str) -> tuple[int, int]:
    """Locate a required control field and its continuation lines."""
    start = next((index for index, line in enumerate(lines) if line.startswith(field + ':')), None)
    if start is None:
        raise ValueError(f'control paragraph has no {field} field')

    end = _control_field_end(lines, start)
    return start, end


def _control_field_end(lines: list[str], start: int) -> int:
    """Find the first line after a control field and its continuations."""
    end = start + 1
    while end < len(lines) and lines[end].startswith((' ', '\t')):
        end += 1

    return end


def _dependency_insert_position(lines: list[str], start: int, end: int, package: str) -> int:
    """Place concrete dependencies before substitutions in lexical order."""
    insert = end
    for index in range(start + 1, end):
        token = lines[index].strip().split(maxsplit=1)[0].rstrip(',') if lines[index].strip() else ''
        if token.startswith('${') or token.lower() > package:
            insert = index
            break

    return insert


def _existing_command_owners(baseline: list[dict], debian: Path) -> set[Path]:
    """Infer candidate binary owners from previously declared executable entry points."""
    old_commands = {item['name'] for item in baseline
                    if item['group'] in EXECUTABLE_ENTRY_POINT_GROUPS}
    existing_owners = {owner for command in old_commands
                       for owner in _manifest_owners(debian, f'usr/bin/{command}')}
    return existing_owners


@dataclass(frozen=True)
class EntryPointEdit:
    """One entry-point finding and its optional manifest adaptation action."""

    finding: dict
    action: dict | None = None


class EntryPointEditor:
    """Own manifest location and the baseline evidence permitting command assignment."""

    def __init__(self, debian: Path, baseline: list[dict]) -> None:
        """Infer owners from existing commands without creating binary packages."""
        self.debian = debian
        self.existing_owners = _existing_command_owners(baseline, debian)

    def adapt(self, introduced: list[dict]) -> tuple[list[dict], list[dict]]:
        """Return findings and actions in declaration order after conservative edits."""
        findings = []
        actions = []
        for item in introduced:
            edit = self.inspect(item)
            findings.append(edit.finding)
            if edit.action is not None:
                actions.append(edit.action)

        return findings, actions

    def inspect(self, item: dict) -> EntryPointEdit:
        """Classify one declaration, keeping the caller's entry-point record unchanged."""
        finding = {**item, 'human_binary_package_review_required': True}
        if item['group'] in EXECUTABLE_ENTRY_POINT_GROUPS:
            finding['kind'] = 'command-or-service'
            return self._command(finding)

        finding.update(kind='plugin-entry-point', packaging='included-with-python-metadata')
        return EntryPointEdit(finding)

    def _command(self, finding: dict) -> EntryPointEdit:
        """Keep covered commands and require one baseline owner for missing commands."""
        installed_path = f"usr/bin/{finding['name']}"
        finding['installed_path'] = installed_path
        owners = _manifest_owners(self.debian, installed_path)
        if owners:
            finding.update(packaging='already-covered', owning_manifests=[path.name for path in owners])
            return EntryPointEdit(finding)

        if len(self.existing_owners) == 1:
            return self._install(finding, next(iter(self.existing_owners)))

        finding.update(packaging='human-decision-required',
                       candidate_manifests=sorted(path.name for path in self.existing_owners))
        return EntryPointEdit(finding)

    def _install(self, finding: dict, owner: Path) -> EntryPointEdit:
        """Append a command to its unambiguous owner and return the full edit evidence."""
        installed_path = finding['installed_path']
        with owner.open('a') as stream:
            if owner.stat().st_size and not owner.read_text().endswith('\n'):
                stream.write('\n')

            stream.write(installed_path + '\n')

        finding.update(packaging='assigned-to-existing-binary', owning_manifests=[owner.name])
        action = {
            'action': 'install-new-upstream-command',
            'name': finding['name'], 'entry_point_group': finding['group'],
            'target': finding['target'], 'installed_path': installed_path,
            'manifest': owner.name,
            'reason': ('A new upstream executable was not covered by packaging; '
                       'existing commands identify one unambiguous binary owner.'),
            'human_binary_package_review_required': True,
        }
        return EntryPointEdit(finding, action)









def _project_group_entries(group: str, declarations: dict) -> list[dict]:
    """Normalize one PEP 621 entry-point group into review records."""
    result = []
    for name, target in declarations.items():
        result.append({'group': group.replace('-', '_'), 'name': name,
                       'target': str(target), 'declared_in': 'pyproject.toml'})

    return result


def _usable_dependency_records(records: list[dict]) -> list[dict]:
    """Select only archive-proven test and build-system dependencies."""
    usable = [record for record in records
              if record.get('kind') in {'test', 'build-system'}
              and record.get('archive_binary') and record.get('archive_satisfies') is True]
    return usable


def _existing_build_dependencies(source: str) -> set[str]:
    """Read all source build dependency fields, including continuations."""
    build_values = []
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r'^Build-Depends(?:-Indep|-Arch)?:', line):
            continue

        value = _control_field_value(lines, index, line)
        build_values.append(value)

    build_names = dependency_names(', '.join(build_values))
    return build_names


def _control_field_value(lines: list[str], index: int, line: str) -> str:
    """Join one dependency field with its indented continuation lines."""
    value = line.split(':', 1)[1]
    cursor = index + 1
    while cursor < len(lines) and lines[cursor].startswith((' ', '\t')):
        value += '\n' + lines[cursor].strip()
        cursor += 1

    return value


class BuildDependencyEditor:
    """Own source control text and the dependency inventory changed by adaptations."""

    def __init__(self, source: str) -> None:
        """Capture all build dependency fields and the default location for test tools."""
        self.source = source
        self.build_names = _existing_build_dependencies(source)
        self.build_field = 'Build-Depends-Indep' if 'Build-Depends-Indep:' in source else 'Build-Depends'

    def adapt(self, grouped: dict[str, list[dict]]) -> list[dict]:
        """Add proven requirements in package order and return only mutation actions."""
        actions = []
        for package, requirements in sorted(grouped.items()):
            action = self.add(package, requirements)
            if action is not None:
                actions.append(action)

        return actions

    def add(self, package: str, requirements: list[dict]) -> dict | None:
        """Add one absent package, keeping control text and the name inventory together."""
        if package in self.build_names:
            return None

        kinds = {record['kind'] for record in requirements}
        field = 'Build-Depends' if 'build-system' in kinds else self.build_field
        self.source = _add_control_dependency(self.source, field, package)
        self.build_names.add(package)
        return {
            'action': 'add-upstream-dependency',
            'package': package,
            'source': requirements[0]['archive_source'],
            'archive_version': requirements[0]['archive_version'],
            'requirement_kinds': sorted(kinds),
            'scopes': ['build'],
            'evidence': _requirement_evidence(requirements),
            'reason': ('Pinned upstream dependency is absent from Debian packaging; '
                       'the Ubuntu archive version satisfies the declared constraint.'),
        }



def _requirement_evidence(requirements: list[dict]) -> list[str]:
    """Sort and deduplicate the upstream declarations supporting an adaptation."""
    evidence = sorted({
        f"{record['file']}:{record['line']} {record['requirement']}"
        for record in requirements
    })
    return evidence


def _version_probe_adjustments(control: Path) -> list[dict]:
    """Allow warnings for existing superficial module-version tests only."""
    actions = []
    if control.is_file():
        before = control.read_text()
        blocks = re.split(r'(\n\s*\n)', before)
        for index in range(0, len(blocks), 2):
            revised_block = _version_probe_block(blocks[index])
            if revised_block != blocks[index]:
                blocks[index] = revised_block

        revised = ''.join(blocks)
        if revised != before:
            control.write_text(revised)
            actions.append({
                'action': 'allow-superficial-version-warning',
                'path': 'debian/tests/control',
                'reason': ('A version-probe autopkgtest accesses a deprecated module attribute; '
                           'stderr warnings do not invalidate its exit-status check.'),
            })

    return actions


def _version_probe_block(block: str) -> str:
    """Add allow-stderr to a module-version probe without altering other tests."""
    if 'Test-Command:' not in block or '.__version__' not in block:
        return block

    block = _version_probe_restrictions(block)
    return block


def _version_probe_restrictions(block: str) -> str:
    """Preserve restrictions while adding the missing warning allowance."""
    restrictions = re.search(r'(?m)^Restrictions:\s*(.*)$', block)
    if restrictions and 'allow-stderr' not in {
            item.strip() for item in restrictions.group(1).split(',')}:
        replacement = restrictions.group(0) + ', allow-stderr'
        block = block[:restrictions.start()] + replacement + block[restrictions.end():]
    elif not restrictions:
        block = block.rstrip('\n') + '\nRestrictions: allow-stderr\n'

    return block


def _uses_python_autopkgtest(entry: dict, tree: Path) -> bool:
    """Recognize explicit Python autopkgtests and conventional Python source names."""
    source_control = fields(tree / 'debian' / 'control')
    source = entry['source']
    if (source_control.get('Testsuite') != 'autopkgtest-pkg-python'
            and not re.fullmatch(r'python3?-.+', source)):
        return False

    return True


def _top_level_imports(tree: Path) -> set[str]:
    """Collect valid import names declared by generated egg metadata."""
    names = set()
    for top_level in tree.glob('*.egg-info/top_level.txt'):
        names.update(line.strip() for line in top_level.read_text().splitlines()
                     if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', line.strip()))

    return names


def _autodep8_import_adjustments(entry: dict, tree: Path, tests: Path) -> list[dict]:
    """Configure autodep8 only for one unambiguous import and no existing config."""
    actions = []
    if not _uses_python_autopkgtest(entry, tree):
        return actions

    source = entry['source']
    expected = _source_import_name(source)
    names = _top_level_imports(tree)
    if expected in names or len(names) != 1:
        return actions

    actual = next(iter(names))
    tests.mkdir(parents=True, exist_ok=True)
    config = tests / 'autopkgtest-pkg-python.conf'
    if _path_occupied(config):
        return actions

    config.write_text(f'import_name = {actual}\n')
    actions.append({
        'action': 'set-autodep8-python-import',
        'path': 'debian/tests/autopkgtest-pkg-python.conf',
        'expected_import': expected, 'actual_import': actual,
        'reason': 'The source-package-derived autodep8 import is absent from upstream metadata.',
    })
    return actions


def _source_import_name(source: str) -> str:
    """Derive the import name assumed by Debian's Python autopkgtest generator."""
    prefix = 'python3-' if source.startswith('python3-') else 'python-'
    return source.removeprefix(prefix).replace('-', '_')


def _path_occupied(path: Path) -> bool:
    """Treat dangling symlinks as occupied paths when preserving user configuration."""
    return path.exists() or path.is_symlink()


def _group_archive_requirements(usable: list[dict]) -> dict[str, list[dict]]:
    """Group proven requirements by their selected binary package."""
    grouped: dict[str, list[dict]] = {}
    for record in usable:
        grouped.setdefault(record['archive_binary'], []).append(record)

    return grouped
