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
import subprocess
from urllib.parse import urlparse

ALIASES = {'keystoneauth': 'python-keystoneauth1',
           'puppet-openstack_extras': 'puppet-module-openstack-extras'}


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


def repository_names(metadata: dict) -> list[str]:
    names = set(metadata.get('repository-settings', {}))
    for release in metadata.get('releases', []):
        names.update(p['repo'] for p in release.get('projects', []))
    return sorted(names)


def package_record(name: str, metadata: dict, archive: dict, *, series: str, membership: str) -> dict:
    repos = repository_names(metadata)
    # Multi-repository deliverables need an explicit mapping; never guess that
    # a deliverable filename is necessarily the source repository name.
    exact = [repo for repo in repos if repo.rsplit('/', 1)[-1] == name]
    repo = exact[0] if len(exact) == 1 else repos[0] if len(repos) == 1 else None
    branches = {branch['name'] for branch in metadata.get('branches', [])}
    branch = f'stable/{series}' if f'stable/{series}' in branches else 'master'
    checksums = [line.split() for line in archive['Checksums-Sha256'].splitlines() if line.strip()]
    dsc = next((row for row in checksums if row[2].endswith('.dsc')), None)
    if dsc is None:
        raise ValueError(f'No .dsc checksum for {archive["Package"]}')
    return {
        'deliverable': name, 'source': archive['Package'], 'membership': membership,
        'release_type': metadata.get('type', 'other'), 'team': metadata.get('team'),
        'upstream_repository': 'https://opendev.org/' + repo if repo else None,
        'upstream_ref': branch,
        'branch_policy': 'release-metadata-stable' if branch != 'master' else 'release-metadata-no-stable-branch',
        'all_upstream_repositories': repos,
        'archive_version': archive['Version'],
        'archive_identity_evidence': identity_evidence(archive),
        'binaries': sorted(x.strip() for x in archive.get('Binary', '').split(',') if x.strip()),
        'build_depends': ', '.join(archive.get(k, '') for k in ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch') if archive.get(k)),
        'testsuite': archive.get('Testsuite', ''),
        'archive_packaging_repository': archive.get('Vcs-Git'),
        'packaging_repository': f'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/{archive["Package"]}',
        'packaging_branch_candidates': [f'stable/{series}', 'master'],
        'archive_source': {'url': 'https://archive.ubuntu.com/ubuntu/' + archive['Directory'] + '/' + dsc[2], 'sha256': dsc[0]},
        'snapshot_backend': 'git-archive' if name.startswith('puppet-') else 'python-sdist',
        'discovery_error': None if repo else 'Multiple or missing upstream repositories require explicit mapping',
    }


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
    cycle = {path.stem: yaml.safe_load(path.read_text()) for path in sorted((releases / 'deliverables' / codename).glob('*.yaml'))}
    if not cycle:
        raise ValueError(f'No deliverables for {codename}')
    independent = {path.stem: yaml.safe_load(path.read_text()) for path in sorted((releases / 'deliverables' / '_independent').glob('*.yaml'))}
    packages = {}
    exclusions = []
    for name, metadata in cycle.items():
        source = source_name(name, sources)
        if source:
            packages[source] = package_record(name, metadata, sources[source], series=series, membership='cycle')
        else:
            exclusions.append({'deliverable': name, 'release_type': metadata.get('type'),
                               'repositories': repository_names(metadata),
                               'reason': f'No source package with verified OpenStack Homepage/VCS identity in Ubuntu {suite} source indexes'})
    candidates = {}
    for name, metadata in independent.items():
        source = source_name(name, sources)
        if source and source not in packages:
            candidates[source] = package_record(name, metadata, sources[source], series=series, membership='independent-build-dependency')
    binary_sources = {binary: source for source, item in {**candidates, **packages}.items() for binary in item['binaries']}
    while True:
        required = {binary_sources[binary] for item in packages.values() for binary in dependency_names(item['build_depends']) if binary in binary_sources}
        additions = required - packages.keys()
        if not additions:
            break
        packages.update({source: candidates[source] for source in additions})
    for source, item in packages.items():
        item['build_dependencies'] = sorted({binary_sources[binary] for binary in dependency_names(item['build_depends']) if binary in binary_sources and binary_sources[binary] in packages} - {source})
    revision = subprocess.check_output(['git', '-C', str(releases), 'rev-parse', 'HEAD'], text=True).strip()
    return {'schema_version': 1, 'series': series, 'codename': codename, 'suite': suite,
            'release_metadata': {'repository': 'https://opendev.org/openstack/releases', 'sha': revision},
            'archive_indexes': indexes,
            'scope': 'All cycle deliverables mapped to Ubuntu main/universe source packages, plus transitive independently released OpenStack build dependencies',
            'packages': sorted(packages.values(), key=lambda item: item['source']), 'exclusions': exclusions}


def write_catalog(catalog: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(catalog, indent=2) + '\n')
