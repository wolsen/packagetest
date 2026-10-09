"""Auditable OpenStack release scope joined to Ubuntu source indexes.

The archive indexes establish package names, not release membership. Membership
comes from the pinned OpenStack releases checkout. Nothing silently disappears
when Ubuntu has no source package for a deliverable.
"""
from __future__ import annotations

import hashlib
import json
import lzma
from pathlib import Path
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ALIASES = {'keystoneauth': 'python-keystoneauth1',
           'puppet-openstack_extras': 'puppet-module-openstack-extras'}

# Explicit upstream migrations/retirements; archive dependencies still need a
# source snapshot, but a tombstone branch is not usable source code.
UPSTREAM_OVERRIDES = {
    'python-gnocchiclient': {
        'upstream_repository': 'https://github.com/gnocchixyz/python-gnocchiclient',
        'upstream_ref': 'master', 'branch_policy': 'maintained-upstream-moved-from-opendev',
        'snapshot_version_backend': 'setuptools-scm',
    },
    'python-requestsexceptions': {
        'upstream_ref': 'bb64d8a07b515947cf000c375b017026a01f7a4f',
        'branch_policy': 'last-source-commit-before-upstream-retirement',
    },
}

# Ubuntu source metadata can retain an obsolete Debian Vcs-Git path after the
# OpenStack packaging team reorganizes Salsa subgroups.
PACKAGING_REPOSITORY_OVERRIDES = {
    'python-cyborgclient': 'https://salsa.debian.org/openstack-team/clients/python-cyborgclient.git',
}

ARCHIVE_COMPONENTS = ('main', 'universe')


def ubuntu_source_indexes(suite: str) -> list[dict[str, str]]:
    """Return the live Ubuntu source indexes that define a nightly baseline."""
    indexes = []
    for pocket, host in (('', 'https://archive.ubuntu.com/ubuntu'),
                         ('-updates', 'https://archive.ubuntu.com/ubuntu'),
                         ('-security', 'https://security.ubuntu.com/ubuntu')):
        distribution = suite + pocket
        for component in ARCHIVE_COMPONENTS:
            indexes.append({
                'filename': f'packagetest-{distribution}-{component}-Sources.xz',
                'url': f'{host}/dists/{distribution}/{component}/source/Sources.xz',
            })
    return indexes


def download_source_indexes(suite: str, destination: Path, *, opener=None) -> list[Path]:
    """Download current indexes atomically instead of relying on committed output."""
    opener = opener or urlopen
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for spec in ubuntu_source_indexes(suite):
        path = destination / spec['filename']
        temporary = path.with_suffix(path.suffix + '.part')
        request = Request(spec['url'], headers={'User-Agent': 'packagetest-catalog/1'})
        with opener(request, timeout=90) as response, temporary.open('wb') as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        temporary.replace(path)
        paths.append(path)
    return paths


def validate_archive_sources(catalog: dict, *, opener=None, max_workers: int = 24) -> None:
    """Fail planning when any source file selected from the fresh index is absent."""
    opener = opener or urlopen
    files = []
    for package in catalog['packages']:
        source = package['archive_source']
        records = source.get('files') or [{'url': source['url'], 'size': None}]
        files.extend((package['source'], record['url'], record.get('size')) for record in records)

    def check(item):
        source, url, expected_size = item
        request = Request(url, headers={'User-Agent': 'packagetest-catalog/1'}, method='HEAD')
        try:
            with opener(request, timeout=30) as response:
                size = response.headers.get('Content-Length')
                if expected_size is not None and size is not None and int(size) != expected_size:
                    raise ValueError(f'expected {expected_size} bytes, archive reports {size}')
        except HTTPError as exc:
            if exc.code not in {405, 501}:
                raise
            request = Request(url, headers={'User-Agent': 'packagetest-catalog/1',
                                            'Range': 'bytes=0-0'})
            with opener(request, timeout=30):
                pass
        return source, url

    failures = []
    with ThreadPoolExecutor(max_workers=max_workers) as workers:
        futures = {workers.submit(check, item): item for item in files}
        for future, item in futures.items():
            try:
                future.result()
            except Exception as exc:
                failures.append(f'{item[0]}: {item[1]}: {exc}')
    if failures:
        details = '\n'.join(failures[:20])
        suffix = f'\n... and {len(failures) - 20} more' if len(failures) > 20 else ''
        raise ValueError(f'{len(failures)} Ubuntu source files failed preflight:\n{details}{suffix}')


def paragraphs(text: str) -> list[dict[str, str]]:
    result = []
    for paragraph in text.split('\n\n'):
        item = {}
        key = None
        for line in paragraph.splitlines():
            if line[:1].isspace() and key:
                item[key] += '\n' + line.strip()
            elif ':' in line:
                key, value = line.split(':', 1)
                item[key] = value.strip()
        if item:
            result.append(item)
    return result


def dependency_names(expression: str) -> set[str]:
    """Conservative names across alternatives, architectures and build profiles.

    This is discovery, not an APT solver. Retaining every alternative prevents
    an OpenStack producer from being omitted; the actual solver checks versions.
    """
    expression = re.sub(r'\([^)]*\)|\[[^]]*\]|<[^>]*>', '', expression)
    return {match.group(1) for group in re.split('[,|]', expression)
            if (match := re.match(r'\s*([a-z0-9][a-z0-9+.-]*)', group))}


def identity_evidence(archive: dict) -> dict[str, str]:
    """Require OpenStack provenance; package-name equality alone is unsafe.

    Ubuntu also ships unrelated genomics Bifrost, Java Trove, and C++ Taskflow.
    VCS ownership and upstream project namespaces distinguish those packages.
    """
    evidence = {}
    for field in ('Homepage', 'Vcs-Git', 'Vcs-Browser'):
        value = archive.get(field, '')
        parsed = urlparse(value.split(' ', 1)[0])
        host, path = parsed.hostname, parsed.path
        approved = (
            host == 'salsa.debian.org' and path.startswith('/openstack-team/') or
            host == 'anonscm.debian.org' and path.startswith('/git/openstack/') or
            host == 'git.launchpad.net' and path.startswith('/~ubuntu-openstack-dev/') or
            host in {'opendev.org', 'github.com', 'git.openstack.org'} and path.startswith('/openstack/') or
            host in {'docs.openstack.org', 'www.openstack.org', 'openstack.org'}
        )
        if approved:
            evidence[field] = value
    return evidence


def source_name(deliverable: str, sources: dict) -> str | None:
    for candidate in (ALIASES.get(deliverable), deliverable, 'python-' + deliverable,
                      'openstack-' + deliverable,
                      deliverable.replace('puppet-', 'puppet-module-', 1)):
        if candidate in sources and identity_evidence(sources[candidate]):
            return candidate
    return None


def vcs_git(value: str) -> tuple[str | None, str | None]:
    """Split a Debian Vcs-Git URL and its optional ``-b`` branch hint."""
    words = shlex.split(value or '')
    if not words:
        return None, None
    branch = words[words.index('-b') + 1] if '-b' in words and words.index('-b') + 1 < len(words) else None
    return words[0], branch


def repository_names(metadata: dict) -> list[str]:
    names = set(metadata.get('repository-settings', {}))
    for release in metadata.get('releases', []):
        names.update(p['repo'] for p in release.get('projects', []))
    return sorted(names)


def _upstream_selection(metadata: dict, repository: str | None, *, series: str,
                        membership: str, status: str) -> tuple[str, str, str | None]:
    """Select the live series branch or the best series-scoped immutable ref."""
    stable = f'stable/{series}'
    branches = {branch['name'] for branch in metadata.get('branches', [])}
    if stable in branches:
        return stable, 'release-metadata-stable', None
    if status == 'development':
        return 'master', 'release-metadata-development-branch', None
    # Cycle deliverable files contain only releases assigned to this series.
    # Independent deliverables span many series and need upper-constraints
    # selection before one of their release hashes can safely be chosen.
    if membership == 'cycle' and repository:
        for release in reversed(metadata.get('releases', [])):
            for project in release.get('projects', []):
                revision = project.get('hash')
                if (project.get('repo') == repository
                        and isinstance(revision, str)
                        and re.fullmatch(r'[0-9a-f]{40}', revision)):
                    return revision, 'release-metadata-series-release', release.get('version')
    return 'master', 'release-metadata-no-series-ref', None


def package_record(name: str, metadata: dict, archive: dict, *, series: str,
                   membership: str, series_status: str = 'unknown',
                   codename: str | None = None) -> dict:
    repos = repository_names(metadata)
    # Multi-repository deliverables need an explicit mapping; never guess that
    # a deliverable filename is necessarily the source repository name.
    exact = [repo for repo in repos if repo.rsplit('/', 1)[-1] == name]
    repo = exact[0] if len(exact) == 1 else repos[0] if len(repos) == 1 else None
    upstream_ref, branch_policy, upstream_release = _upstream_selection(
        metadata, repo, series=series, membership=membership, status=series_status)
    checksums = [line.split() for line in archive['Checksums-Sha256'].splitlines() if line.strip()]
    base_url = 'https://archive.ubuntu.com/ubuntu/' + archive['Directory'] + '/'
    archive_files = [{'name': row[2], 'url': base_url + row[2],
                      'sha256': row[0], 'size': int(row[1])} for row in checksums]
    dsc = next((row for row in archive_files if row['name'].endswith('.dsc')), None)
    if dsc is None:
        raise ValueError(f'No .dsc checksum for {archive["Package"]}')
    archive_packaging_repository, archive_packaging_branch = vcs_git(archive.get('Vcs-Git', ''))
    archive_packaging_repository = PACKAGING_REPOSITORY_OVERRIDES.get(
        archive['Package'], archive_packaging_repository)
    return {
        'deliverable': name, 'source': archive['Package'], 'membership': membership,
        'release_type': metadata.get('type', 'other'), 'team': metadata.get('team'),
        'upstream_repository': 'https://opendev.org/' + repo if repo else None,
        'upstream_ref': upstream_ref,
        'branch_policy': branch_policy,
        'upstream_release': upstream_release,
        'series_status': series_status,
        'all_upstream_repositories': repos,
        'archive_version': archive['Version'],
        'archive_identity_evidence': identity_evidence(archive),
        'binaries': sorted(x.strip() for x in archive.get('Binary', '').split(',') if x.strip()),
        'build_depends': ', '.join(archive.get(k, '') for k in ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch') if archive.get(k)),
        'testsuite': archive.get('Testsuite', ''),
        'archive_packaging_repository': archive_packaging_repository,
        'archive_packaging_branch': archive_packaging_branch,
        'packaging_repository': f'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/{archive["Package"]}',
        'packaging_branch_candidates': [f'stable/{series}', 'master'],
        'packaging_upstream_branch_candidates': [f'upstream-{codename or series_name(series)}', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
        'archive_source': {'url': dsc['url'], 'sha256': dsc['sha256'],
                           'files': archive_files},
        'snapshot_backend': 'git-archive' if name.startswith('puppet-') else 'python-sdist',
        'discovery_error': None if repo else 'Multiple or missing upstream repositories require explicit mapping',
    }


def series_name(series: str) -> str:
    """Map the active numeric OpenStack series to its release codename."""
    # Keep this explicit: guessing a future codename would select the wrong
    # packaging upstream branch while still producing a syntactically valid ref.
    names = {'2026.2': 'hibiscus', '2027.1': 'indri'}
    if series not in names:
        raise ValueError(f'No packaging upstream branch mapping for OpenStack {series}')
    return names[series]


def archive_python_packages(sources: dict[str, dict]) -> list[dict]:
    """Retain Ubuntu's Python binary providers for upstream dependency checks.

    The OpenStack catalog deliberately contains only release deliverables and
    their OpenStack build closure.  Test requirements such as ``gabbi`` and
    ``wsgi-intercept`` are supplied by unrelated Ubuntu source packages, so a
    second lightweight index is needed to prove their availability without
    adding them to the OpenStack build DAG.
    """
    records = []
    for source, item in sorted(sources.items()):
        for binary in sorted(x.strip() for x in item.get('Binary', '').split(',') if x.strip()):
            if not re.fullmatch(r'python3-[a-z0-9][a-z0-9+.-]*', binary):
                continue
            records.append({
                'distribution': re.sub(r'[-_.]+', '-', binary.removeprefix('python3-')).lower(),
                'binary': binary,
                'source': source,
                'version': item['Version'],
            })
    return records


def make_catalog(releases: Path, source_indexes: list[Path], *, series='2026.2', codename='hibiscus', suite='resolute') -> dict:
    # YAML is needed only for refreshing the catalog, not for consuming it.
    import yaml
    sources = {}
    indexes = []
    for path in source_indexes:
        data = path.read_bytes()
        indexes.append({'filename': path.name, 'sha256': hashlib.sha256(data).hexdigest()})
        text = lzma.decompress(data).decode() if path.suffix == '.xz' else data.decode()
        for source in paragraphs(text):
            if 'Package' in source:
                previous = sources.get(source['Package'])
                if previous is None or subprocess.run(['dpkg', '--compare-versions', source['Version'], 'gt', previous['Version']]).returncode == 0:
                    sources[source['Package']] = source
    status_path = releases / 'data' / 'series_status.yaml'
    statuses = yaml.safe_load(status_path.read_text()) if status_path.is_file() else []
    status_record = next((item for item in statuses
                          if item.get('name') == codename
                          or str(item.get('release-id')) == str(series)), {})
    target_status = status_record.get('status', 'unknown')
    cycle = {path.stem: yaml.safe_load(path.read_text()) for path in sorted((releases / 'deliverables' / codename).glob('*.yaml'))}
    if not cycle:
        raise ValueError(f'No deliverables for {codename}')
    independent = {path.stem: yaml.safe_load(path.read_text()) for path in sorted((releases / 'deliverables' / '_independent').glob('*.yaml'))}
    packages = {}
    exclusions = []
    for name, metadata in cycle.items():
        source = source_name(name, sources)
        if source:
            packages[source] = package_record(
                name, metadata, sources[source], series=series, membership='cycle',
                series_status=target_status, codename=codename)
        else:
            exclusions.append({'deliverable': name, 'release_type': metadata.get('type'),
                               'repositories': repository_names(metadata),
                               'reason': f'No source package with verified OpenStack Homepage/VCS identity in Ubuntu {suite} source indexes'})
    candidates = {}
    for name, metadata in independent.items():
        source = source_name(name, sources)
        if source and source not in packages:
            candidates[source] = package_record(
                name, metadata, sources[source], series=series,
                membership='independent-build-dependency',
                series_status=target_status, codename=codename)
    binary_sources = {binary: source for source, item in {**candidates, **packages}.items() for binary in item['binaries']}
    while True:
        required = {binary_sources[binary] for item in packages.values() for binary in dependency_names(item['build_depends']) if binary in binary_sources}
        additions = required - packages.keys()
        if not additions:
            break
        packages.update({source: candidates[source] for source in additions})
    for source, item in packages.items():
        item.update(UPSTREAM_OVERRIDES.get(source, {}))
        item['build_dependencies'] = sorted({binary_sources[binary] for binary in dependency_names(item['build_depends']) if binary in binary_sources and binary_sources[binary] in packages} - {source})
    revision = subprocess.check_output(['git', '-C', str(releases), 'rev-parse', 'HEAD'], text=True).strip()
    return {'schema_version': 1, 'series': series, 'codename': codename,
            'series_status': target_status, 'suite': suite,
            'release_metadata': {'repository': 'https://opendev.org/openstack/releases', 'sha': revision},
            'archive_indexes': indexes,
            'archive_python_indexes': indexes,
            'archive_python_packages': archive_python_packages(sources),
            'scope': 'All cycle deliverables mapped to Ubuntu main/universe source packages, plus transitive independently released OpenStack build dependencies',
            'packages': sorted(packages.values(), key=lambda item: item['source']), 'exclusions': exclusions}


def write_catalog(catalog: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(catalog, indent=2) + '\n')
