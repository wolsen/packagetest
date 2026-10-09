"""Prepare independently buildable, pinned snapshot source packages.

The Ubuntu OpenStack Launchpad Git tree normally supplies Debian packaging;
the archive Vcs-Git tree covers packages not maintained there. Upstream Git
supplies the new source tree. A checksum-pinned published source package is an
explicit final fallback when neither packaging repository can be resolved.
"""
from __future__ import annotations

import configparser
from datetime import datetime, timezone
import fnmatch
import gzip
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .artifacts import checksum_entries, fields, sha256, verify_source
from .catalog import dependency_names
from .commands import CommandRunner
from .snapshot import build_snapshot
from .snapshot_lock import select_commit

BUILD_REQUIREMENTS = [
    {'name': 'pbr', 'version': '7.0.3', 'sha256': 'ff223894eb1cd271a98076b13d3badff3bb36c424074d26334cd25aebeecea6b'},
    {'name': 'setuptools', 'version': '80.9.0', 'sha256': '062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922'},
    {'name': 'wheel', 'version': '0.45.1', 'sha256': '708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248'},
]
SCM_REQUIREMENTS = [
    {'name': 'setuptools-scm', 'version': '8.3.1', 'sha256': '332ca0d43791b818b841213e76b1971b7711a960761c5bea5fc5cdb5196fbce3'},
    {'name': 'packaging', 'version': '25.0', 'sha256': '29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484'},
]


def _entry_points_from_text(setup_cfg: str | None, pyproject: str | None) -> list[dict]:
    """Return statically declared Python entry points in a stable form."""
    result = []
    if setup_cfg:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(setup_cfg)
        except configparser.Error as exc:
            raise ValueError(f'Invalid setup.cfg while inspecting entry points: {exc}') from exc
        if parser.has_section('entry_points'):
            for group, value in parser.items('entry_points'):
                for line in value.splitlines():
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        name, target = (part.strip() for part in line.split('=', 1))
                        result.append({'group': group.replace('-', '_'), 'name': name,
                                       'target': target, 'declared_in': 'setup.cfg'})
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
            for name, target in declarations.items():
                result.append({'group': group.replace('-', '_'), 'name': name,
                               'target': str(target), 'declared_in': 'pyproject.toml'})
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
    archive_upstream = _archive_upstream_version(entry['archive_version'])
    from .versioning import upstream_version_to_debian_version
    tags = subprocess.run(
        ['git', '-C', str(checkout), 'tag', '--merged', selected['sha']],
        text=True, capture_output=True, check=True).stdout.splitlines()
    matches = []
    for tag in tags:
        normalized = upstream_version_to_debian_version(tag).rsplit('-', 1)[0]
        if normalized == archive_upstream:
            matches.append(tag)
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
    executable_groups = {'console_scripts', 'gui_scripts', 'wsgi_scripts'}
    old_commands = {item['name'] for item in baseline if item['group'] in executable_groups}
    debian = tree / 'debian'
    existing_owners = {owner for command in old_commands
                       for owner in _manifest_owners(debian, f'usr/bin/{command}')}
    actions = []
    findings = []
    for item in introduced:
        finding = {**item, 'kind': ('command-or-service' if item['group'] in executable_groups
                                    else 'plugin-entry-point'),
                   'human_binary_package_review_required': True}
        if item['group'] in executable_groups:
            installed_path = f"usr/bin/{item['name']}"
            owners = _manifest_owners(debian, installed_path)
            finding['installed_path'] = installed_path
            if owners:
                finding.update(packaging='already-covered',
                               owning_manifests=[path.name for path in owners])
            elif len(existing_owners) == 1:
                owner = next(iter(existing_owners))
                with owner.open('a') as stream:
                    if owner.stat().st_size and not owner.read_text().endswith('\n'):
                        stream.write('\n')
                    stream.write(installed_path + '\n')
                finding.update(packaging='assigned-to-existing-binary',
                               owning_manifests=[owner.name])
                actions.append({
                    'action': 'install-new-upstream-command',
                    'name': item['name'], 'entry_point_group': item['group'],
                    'target': item['target'], 'installed_path': installed_path,
                    'manifest': owner.name,
                    'reason': ('A new upstream executable was not covered by packaging; '
                               'existing commands identify one unambiguous binary owner.'),
                    'human_binary_package_review_required': True,
                })
            else:
                finding.update(packaging='human-decision-required',
                               candidate_manifests=sorted(path.name for path in existing_owners))
        else:
            finding['packaging'] = 'included-with-python-metadata'
        findings.append(finding)

    commit_delta = _official_commit_delta(entry, checkout, selected)
    return ({'schema_version': 1, 'source': entry['source'],
             'commit_delta': commit_delta, 'introduced_entry_points': findings,
             'human_binary_package_review_required': bool(findings)}, actions)


def write_source_evolution(report: dict, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'source-evolution.json').write_text(json.dumps(report, indent=2) + '\n')
    delta = report['commit_delta']
    lines = [f"## {report['source']} upstream evolution", '',
             f"{delta['count']} commits since `{delta['comparison_tag']}` "
             f"({delta['basis'].replace('-', ' ')})."]
    if not delta['comparison_tag_matches_archive_version']:
        lines.extend(['', f"Archive upstream version `{delta['archive_upstream_version']}` did not "
                      'match that tag; verify the official-package baseline.'])
    if delta['commits']:
        lines.extend(['', '| Commit | Subject |', '|---|---|'])
        lines.extend(f"| `{item['sha'][:12]}` | {item['subject'].replace('|', '&#124;')} |"
                     for item in delta['commits'])
    introduced = report['introduced_entry_points']
    if introduced:
        lines.extend(['', '### New commands, services, and entry points', '',
                      'Human review is required to decide whether any item needs a new binary package.', '',
                      '| Group | Name | Packaging |', '|---|---|---|'])
        lines.extend(f"| `{item['group']}` | `{item['name']}` | {item['packaging']} |"
                     for item in introduced)
    else:
        lines.extend(['', 'No new statically declared entry points were detected.'])
    (destination / 'source-evolution.md').write_text('\n'.join(lines) + '\n')


class Preparation:
    def __init__(self, destination: Path):
        self.root = destination.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.runner = CommandRunner(self.root / 'logs' / 'commands.jsonl', stream=True, timeout=1800)

    def command(self, *args, cwd=None, env=None):
        result = self.runner.run(list(args), cwd or self.work, env_diff=env)
        if result.exit_code:
            raise RuntimeError(f'{args[0]} failed ({result.exit_code}): {result.stderr[-2000:]}')
        return (self.runner.log_path.parent / f'{self.runner.sequence:04d}.stdout.log').read_text().strip()


def download_verified(url: str, destination: Path, digest: str) -> None:
    if urlparse(url).scheme != 'https':
        raise ValueError('Pinned source downloads require HTTPS')
    with urlopen(url, timeout=120) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)
    if sha256(destination) != digest:
        raise ValueError(f'Source checksum mismatch: {destination.name}')


def snapshot_debian_version(archive_version: str, upstream: str) -> str:
    epoch = archive_version.split(':', 1)[0] + ':' if ':' in archive_version else ''
    return epoch + upstream + '-0ubuntu1~hibiscus1'



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
    start = next((index for index, line in enumerate(lines) if line.startswith(field + ':')), None)
    if start is None:
        raise ValueError(f'control paragraph has no {field} field')
    end = start + 1
    while end < len(lines) and lines[end].startswith((' ', '\t')):
        end += 1
    current = ''.join(lines[start:end])
    if re.search(rf'(?<![A-Za-z0-9+.-]){re.escape(package)}(?![A-Za-z0-9+.-])', current):
        return text
    insert = end
    for index in range(start + 1, end):
        token = lines[index].strip().split(maxsplit=1)[0].rstrip(',') if lines[index].strip() else ''
        if token.startswith('${') or token.lower() > package:
            insert = index
            break
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
    usable = [record for record in records
              if record.get('kind') in {'test', 'build-system'}
              and record.get('archive_binary') and record.get('archive_satisfies') is True]
    if not usable:
        return []
    path = tree / 'debian' / 'control'
    before = path.read_text()
    paragraphs = re.split(r'(\n\s*\n)', before)
    source = paragraphs[0]
    build_field = 'Build-Depends-Indep' if 'Build-Depends-Indep:' in source else 'Build-Depends'
    build_values = []
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r'^Build-Depends(?:-Indep|-Arch)?:', line):
            continue
        value = line.split(':', 1)[1]
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].startswith((' ', '\t')):
            value += '\n' + lines[cursor].strip()
            cursor += 1
        build_values.append(value)
    build_names = dependency_names(', '.join(build_values))
    actions = []
    grouped: dict[str, list[dict]] = {}
    for record in usable:
        grouped.setdefault(record['archive_binary'], []).append(record)
    for package, requirements in sorted(grouped.items()):
        kinds = {record['kind'] for record in requirements}
        scopes = []
        if package not in build_names:
            field = 'Build-Depends' if 'build-system' in kinds else build_field
            source = _add_control_dependency(source, field, package)
            build_names.add(package)
            scopes.append('build')
        if scopes:
            evidence = sorted({
                f"{record['file']}:{record['line']} {record['requirement']}"
                for record in requirements
            })
            actions.append({
                'action': 'add-upstream-dependency',
                'package': package,
                'source': requirements[0]['archive_source'],
                'archive_version': requirements[0]['archive_version'],
                'requirement_kinds': sorted(kinds),
                'scopes': scopes,
                'evidence': evidence,
                'reason': ('Pinned upstream dependency is absent from Debian packaging; '
                           'the Ubuntu archive version satisfies the declared constraint.'),
            })
    paragraphs[0] = source
    revised = ''.join(paragraphs)
    if revised != before:
        path.write_text(revised)
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
    repositories = []
    for role, key in [('ubuntu', 'packaging_repository'), ('archive', 'archive_packaging_repository')]:
        repository = entry.get(key)
        if repository and repository not in {item['repository'] for item in repositories}:
            repositories.append({'role': role, 'repository': repository})
    if not repositories:
        raise ValueError('Packaging proposal requires a destination repository candidate')
    branches = entry.get('packaging_branch_candidates') or []
    if not branches:
        raise ValueError('Packaging proposal requires destination branch candidates')

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
            'sha256': entry['archive_source']['sha256'],
            'generic_normalization_before_diff': ['ubuntu-maintainer'],
        },
        'patch': {'file': patch_path.name, 'sha256': sha256(patch_path)},
        'actions': actions,
        'removal_condition': 'Retire after the reviewed change is accepted in the selected packaging branch.',
    }
    (output / 'packaging-proposal.json').write_text(json.dumps(proposal, indent=2) + '\n')
    return proposal


def already_applied_patches(tree: Path) -> list[dict]:
    """Omit a quilt patch only if its complete inverse applies without fuzz.

    Also require that the forward patch does not apply, so ambiguous repeated
    contexts do not qualify. Both probes are dry runs; upstream is unchanged.
    """
    series = tree / 'debian/patches/series'
    if not series.exists():
        return []
    lines = series.read_text().splitlines()
    omitted = []
    for index, line in enumerate(lines):
        words = line.split()
        if not words or words[0].startswith('#') or len(words) != 1:
            continue
        name = Path(words[0])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe quilt patch path')
        patch = tree / 'debian/patches' / name
        data = patch.read_bytes()
        args = ['patch', '--dry-run', '--force', '--fuzz=0', '-p1']
        reverse = subprocess.run(args + ['--reverse'], input=data, cwd=tree, capture_output=True)
        if reverse.returncode:
            continue
        forward = subprocess.run(args, input=data, cwd=tree, capture_output=True)
        if forward.returncode:
            lines[index] = '# Fully present upstream (verified reverse dry run): ' + str(name)
            omitted.append({'action': 'omit-already-applied-patch', 'name': str(name),
                            'sha256': sha256(patch), 'reverse_probe': reverse.stdout.decode(errors='replace')})
    if omitted:
        series.write_text('\n'.join(lines) + '\n')
    return omitted


SEMANTIC_PATCH_STOPWORDS = {
    'false', 'from', 'import', 'none', 'return', 'self', 'test', 'true',
    'value', 'with',
}


def _semantic_patch_tokens(value: str) -> set[str]:
    result = set()
    for token in re.findall(r'[A-Za-z][A-Za-z0-9_]{3,}', value):
        token = token.lower()
        if token not in SEMANTIC_PATCH_STOPWORDS:
            result.add(token)
        result.update(part.lower() for part in token.split('_')
                      if len(part) >= 4 and part.lower() not in SEMANTIC_PATCH_STOPWORDS)
    return result


def superseded_upstream_patches(tree: Path, checkout: Path, selected: dict) -> list[dict]:
    """Omit a failed patch when a post-release upstream commit proves equivalence.

    This deliberately requires both the selected source and one recorded commit
    since the base tag to contain at least 70% of the patch's distinctive added
    identifiers. A named Python definition must also survive in current source.
    Borderline cases remain active and are handed to model-assisted review.
    """
    series = tree / 'debian/patches/series'
    if not series.is_file():
        return []
    lines = series.read_text().splitlines()
    adjustments = []
    commits = [item for item in selected.get('commits', [])
               if re.fullmatch(r'[0-9a-f]{40}', item.get('sha', ''))]
    if not commits:
        return []
    for index, line in enumerate(lines):
        words = line.split()
        if len(words) != 1 or words[0].startswith('#'):
            continue
        name = Path(words[0])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe quilt patch path')
        patch = tree / 'debian/patches' / name
        if not patch.is_file() or patch.is_symlink():
            continue
        data = patch.read_bytes()
        probe = ['patch', '--dry-run', '--batch', '--force', '--fuzz=0', '-p1']
        if subprocess.run(probe, input=data, cwd=tree, capture_output=True).returncode == 0:
            continue
        if subprocess.run(probe + ['--reverse'], input=data, cwd=tree,
                          capture_output=True).returncode == 0:
            continue
        text = data.decode(errors='replace')
        targets = []
        for target in re.findall(r'^\+\+\+\s+(?:b/)?([^\t\n ]+)', text, re.M):
            path = Path(target)
            if path.is_absolute() or '..' in path.parts or path.parts[:1] == ('debian',):
                targets = []
                break
            if target not in targets:
                targets.append(target)
        added = '\n'.join(item[1:] for item in text.splitlines()
                          if item.startswith('+') and not item.startswith('+++'))
        tokens = _semantic_patch_tokens(added)
        definitions = set(re.findall(
            r'\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)', added))
        if not targets or len(tokens) < 8 or not definitions:
            continue
        current_parts = []
        for target in targets:
            path = tree / target
            if not path.is_file() or path.is_symlink():
                current_parts = []
                break
            current_parts.append(path.read_text(errors='replace'))
        if not current_parts or not all(any(definition in part for part in current_parts)
                                        for definition in definitions):
            continue
        current_tokens = _semantic_patch_tokens('\n'.join(current_parts))
        current_overlap = len(tokens & current_tokens) / len(tokens)
        if current_overlap < 0.70:
            continue
        best = None
        for commit in commits:
            result = subprocess.run(
                ['git', 'show', '--format=%B', commit['sha'], '--', *targets],
                cwd=checkout, text=True, capture_output=True, timeout=30)
            if result.returncode or not result.stdout.strip():
                continue
            commit_tokens = _semantic_patch_tokens(result.stdout)
            overlap = len(tokens & commit_tokens) / len(tokens)
            if overlap >= 0.70 and (best is None or overlap > best['commit_overlap']):
                best = {'sha': commit['sha'], 'subject': commit.get('subject', ''),
                        'commit_overlap': round(overlap, 3)}
        if best is None:
            continue
        lines[index] = '# Superseded by verified upstream commit ' + best['sha'][:12] + ': ' + str(name)
        adjustments.append({
            'action': 'omit-upstream-superseded-patch', 'name': str(name),
            'sha256': sha256(patch), 'upstream_commit': best,
            'current_overlap': round(current_overlap, 3),
            'token_count': len(tokens), 'targets': targets,
            'human_review_required': True,
        })
    if adjustments:
        series.write_text('\n'.join(lines) + '\n')
    return adjustments

def extract_snapshot(archive: Path, destination: Path) -> None:
    destination.mkdir()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if len(roots) != 1:
            raise ValueError('Snapshot archive must contain exactly one top-level directory')
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise ValueError('Unsafe snapshot archive path')
        # Python data filtering prevents symlinks escaping the extraction root.
        source.extractall(destination, filter='data')
    root = destination / next(iter(roots))
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
        match = re.fullmatch(re.escape(source) + r'_[^/]+\.orig-([A-Za-z0-9-]+)\.tar\.(gz|xz|bz2|lzma)', filename)
        if not match:
            raise ValueError('Unsupported supplementary orig filename: ' + filename)
        component, compression = match.groups()
        archive = baseline_dsc.parent / filename
        if archive.stat().st_size != int(size) or sha256(archive) != digest:
            raise ValueError('Supplementary orig checksum mismatch: ' + filename)
        destination = output / f'{source}_{upstream}.orig-{component}.tar.{compression}'
        component_tree = tree / component
        if destination.exists() or destination.is_symlink() or component_tree.exists() or component_tree.is_symlink():
            raise ValueError('Supplementary orig component collides with snapshot: ' + component)
        extract_snapshot(archive, component_tree)
        shutil.copyfile(archive, destination)
        components.append({'component': component, 'archive_file': filename,
                           'snapshot_file': destination.name, 'sha256': digest})
    return components


def preserve_catalog_orig_components(build: Preparation, entry: dict, upstream: str,
                                     tree: Path, output: Path) -> list[dict]:
    """Restore supplementary orig components while packaging comes from Git."""
    source = entry['source']
    components = []
    directory = build.work / 'supplementary-orig'
    for record in entry['archive_source'].get('files', []):
        filename = record['name']
        if '.orig-' not in filename or filename.endswith('.asc'):
            continue
        match = re.fullmatch(re.escape(source) + r'_[^/]+\.orig-([A-Za-z0-9-]+)\.tar\.(gz|xz|bz2|lzma)', filename)
        if not match:
            raise ValueError('Unsupported supplementary orig filename: ' + filename)
        component, compression = match.groups()
        directory.mkdir(exist_ok=True)
        archive = directory / filename
        download_verified(record['url'], archive, record['sha256'])
        if archive.stat().st_size != record['size']:
            raise ValueError('Supplementary orig size mismatch: ' + filename)
        destination = output / f'{source}_{upstream}.orig-{component}.tar.{compression}'
        component_tree = tree / component
        if destination.exists() or destination.is_symlink() or component_tree.exists() or component_tree.is_symlink():
            raise ValueError('Supplementary orig component collides with snapshot: ' + component)
        extract_snapshot(archive, component_tree)
        shutil.copyfile(archive, destination)
        components.append({'component': component, 'archive_file': filename,
                           'snapshot_file': destination.name,
                           'sha256': record['sha256']})
    return components


def checkout_packaging_tree(build: Preparation, entry: dict) -> tuple[Path, dict]:
    """Checkout the exact source-package Git commit pinned by the run plan."""
    if entry.get('packaging_source_kind') != 'git':
        raise ValueError('Catalog entry does not select Git packaging')
    repository = entry.get('packaging_source_repository')
    revision = entry.get('packaging_sha')
    branch = entry.get('packaging_branch')
    if not repository or not re.fullmatch(r'[a-f0-9]{40}', revision or '') or not branch:
        raise ValueError('Git packaging requires a pinned repository, branch, and commit')
    checkout = build.work / 'packaging'
    build.command('git', 'init', '--quiet', str(checkout))
    build.command('git', 'remote', 'add', 'origin', repository, cwd=checkout)
    build.command('git', 'fetch', '--no-tags', '--depth=1', 'origin', revision, cwd=checkout)
    build.command('git', 'checkout', '--detach', revision, cwd=checkout)
    actual = build.command('git', 'rev-parse', 'HEAD', cwd=checkout)
    if actual != revision:
        raise ValueError('Packaging checkout differs from the plan-pinned commit')
    control = checkout / 'debian' / 'control'
    if not control.is_file() or fields(control).get('Source') != entry['source']:
        raise ValueError('Launchpad packaging debian/control source mismatch')
    changelog_source = build.command('dpkg-parsechangelog', '-S', 'Source', cwd=checkout)
    if changelog_source != entry['source']:
        raise ValueError('Launchpad packaging debian/changelog source mismatch')
    return checkout, {
        'kind': 'git', 'role': entry.get('packaging_source_role'),
        'repository': repository, 'branch': branch, 'sha': revision,
        'upstream_branch': entry.get('packaging_upstream_branch'),
        'upstream_sha': entry.get('packaging_upstream_sha'),
        'pristine_tar_sha': entry.get('packaging_pristine_tar_sha'),
    }


def extract_archive_packaging(build: Preparation, entry: dict) -> tuple[Path, Path, dict]:
    """Extract published packaging only when the run lock records a Git fallback."""
    source = entry['source']
    baseline_dir = build.work / 'baseline'
    baseline_dir.mkdir()
    spec = entry['archive_source']
    baseline_dsc = baseline_dir / Path(urlparse(spec['url']).path).name
    download_verified(spec['url'], baseline_dsc, spec['sha256'])
    for digest, _, filename in checksum_entries(fields(baseline_dsc)):
        download_verified(urljoin(spec['url'], filename), baseline_dir / filename, digest)
    verify_source(baseline_dsc, source, entry['archive_version'])
    packaging_tree = build.work / 'packaging'
    build.command('dpkg-source', '--skip-patches', '-x', str(baseline_dsc), str(packaging_tree))
    return packaging_tree, baseline_dsc, {
        'kind': 'archive', 'url': spec['url'], 'sha256': spec['sha256'],
        'reason': entry.get('packaging_resolution_error'),
    }


def git_archive(build: Preparation, checkout: Path, selected: dict, destination: Path, package: str) -> dict:
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
                   remediation_patch: Path | None = None) -> dict:
    """Resolve and prepare one source; never overwrite an earlier generation.

    Return absolute ``dsc`` and ``lock`` paths plus metadata. Failure leaves
    resolution.json with FAILED and all command logs for artifact upload.
    """
    build = Preparation(destination)
    report = {'source': entry['source'], 'status': 'PREPARING', 'catalog_entry': entry}
    report_path = build.root / 'resolution.json'
    try:
        if (entry.get('discovery_error') or entry.get('upstream_resolution_error') or
                entry.get('upstream_dependency_resolution_error')):
            raise ValueError(entry.get('discovery_error') or entry.get('upstream_resolution_error') or
                             entry['upstream_dependency_resolution_error'])
        if not re.fullmatch(r'[a-f0-9]{40}', entry.get('upstream_sha', '')):
            raise ValueError('Source preparation requires a plan-pinned upstream_sha')
        source = entry['source']
        baseline_dsc = None
        if entry.get('packaging_source_kind') == 'git':
            packaging_tree, packaging_source = checkout_packaging_tree(build, entry)
        else:
            packaging_tree, baseline_dsc, packaging_source = extract_archive_packaging(build, entry)
        report['packaging_source'] = packaging_source
        checkout = build.work / 'upstream-discovery'
        build.command('git', 'clone', '--no-checkout', entry['upstream_repository'], str(checkout))
        # Stable branch is chosen in the catalog from release declarations. A
        # deleted/missing declared branch is an error, never a master fallback.
        build.command('git', 'cat-file', '-e', entry['upstream_sha'] + '^{commit}', cwd=checkout)
        build.command('git', 'update-ref', 'refs/remotes/origin/packagetest-frozen', entry['upstream_sha'], cwd=checkout)
        selected = select_commit(build, checkout, 'packagetest-frozen', None)
        selected.update(ref=entry['upstream_ref'], cutoff=cutoff)
        if selected['sha'] != entry['upstream_sha']:
            raise ValueError('Source preparation changed the plan-pinned upstream SHA')
        upstream = selected['upstream_version']
        version = snapshot_debian_version(entry['archive_version'], upstream)
        build.command('dpkg', '--compare-versions', version, 'gt', entry['archive_version'])
        orig = build.root / f'{source}_{upstream}.orig.tar.gz'
        if entry.get('snapshot_backend') == 'git-archive':
            snapshot = git_archive(build, checkout, selected, orig, source)
        else:
            snapshot_spec = {**selected, 'repository': entry['upstream_repository'],
                             'build_requirements': BUILD_REQUIREMENTS + (SCM_REQUIREMENTS if entry.get('snapshot_version_backend') == 'setuptools-scm' else []),
                             'version_backend': entry.get('snapshot_version_backend', 'pbr'), 'archive_format': 'portable-v1'}
            snapshot = build_snapshot(build, {'source': source, 'input': {'snapshot': snapshot_spec, 'upstream_version': upstream}}, orig)
        tree = build.root / f'{source}-{upstream}'
        extract_snapshot(orig, tree)
        if baseline_dsc is not None:
            report['supplementary_orig_components'] = preserve_orig_components(
                baseline_dsc, source, upstream, tree, build.root)
        else:
            report['supplementary_orig_components'] = preserve_catalog_orig_components(
                build, entry, upstream, tree, build.root)
        if (tree / 'debian').exists():
            raise ValueError('Upstream snapshot unexpectedly contains Debian packaging')
        shutil.copytree(packaging_tree / 'debian', tree / 'debian', symlinks=True)
        ubuntu_maintainer(tree / 'debian' / 'control')
        proposal_baseline = build.work / 'packaging-proposal-baseline'
        shutil.copytree(tree / 'debian', proposal_baseline, symlinks=True)
        evolution, evolution_actions = source_evolution(entry, checkout, selected, tree)
        report['source_evolution'] = evolution
        write_source_evolution(evolution, build.root / 'packaging-proposal')
        report['packaging_adjustments'] = already_applied_patches(tree)
        report['packaging_adjustments'].extend(superseded_upstream_patches(
            tree, checkout, evolution.get('commit_delta', {})))
        report['packaging_adjustments'].extend(upstream_dependency_adjustments(entry, tree))
        report['packaging_adjustments'].extend(evolution_actions)
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
        proposal = write_packaging_proposal(
            entry, proposal_baseline, tree, build.root / 'packaging-proposal',
            report['packaging_adjustments'])
        if proposal:
            report['packaging_proposal'] = proposal
        build.command('dch', '--newversion', version, '--distribution', 'resolute', '--force-distribution',
                      'Nightly OpenStack 2026.2 snapshot from pinned upstream commit ' + selected['sha'] + '.',
                      cwd=tree, env={'DEBFULLNAME': 'Packaging Build Agent', 'DEBEMAIL': 'packaging-agent@example.invalid'})
        build.command('dpkg-buildpackage', '-S', '-d', '-nc', '-us', '-uc', cwd=tree,
                      env={'SOURCE_DATE_EPOCH': selected['commit_timestamp']})
        dscs = list(build.root.glob('*.dsc'))
        if len(dscs) != 1:
            raise ValueError('Expected exactly one generated Debian source package')
        verify_source(dscs[0], source, version)
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
