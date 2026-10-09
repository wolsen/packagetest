import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError

import pytest

from packagetest.catalog import (archive_python_packages, dependency_names, make_catalog,
                                package_record, paragraphs, source_name,
                                ubuntu_source_indexes, validate_archive_sources, vcs_git)


def archive(name, binaries=None, depends=''):
    return {'Package': name, 'Version': '1:2.0-0ubuntu1', 'Binary': binaries or name,
            'Vcs-Git': 'https://salsa.debian.org/openstack-team/libs/' + name + '.git',
            'Build-Depends': depends, 'Directory': 'pool/main/p/' + name,
            'Checksums-Sha256': 'a' * 64 + ' 123 ' + name + '_2.0-0ubuntu1.dsc'}


def test_dependency_names_retains_alternatives_and_strips_qualifiers():
    assert dependency_names('python3-a:any (>= 1) [amd64] <!nocheck>, python3-b | python3-c, debhelper-compat (= 13)') == {'python3-a', 'python3-b', 'python3-c', 'debhelper-compat'}


def test_archive_python_index_keeps_non_openstack_binary_providers():
    sources = {
        'python-wsgi-intercept': archive(
            'python-wsgi-intercept', 'python3-wsgi-intercept, python-wsgi-intercept-doc'),
        'unrelated': archive('unrelated', 'unrelated-bin'),
    }
    assert archive_python_packages(sources) == [{
        'distribution': 'wsgi-intercept',
        'binary': 'python3-wsgi-intercept',
        'source': 'python-wsgi-intercept',
        'version': '1:2.0-0ubuntu1',
    }]


def test_alias_and_unmapped_package():
    assert source_name('keystoneauth', {'python-keystoneauth1': archive('python-keystoneauth1')}) == 'python-keystoneauth1'
    assert source_name('puppet-openstack_extras', {'puppet-module-openstack-extras': archive('puppet-module-openstack-extras')}) == 'puppet-module-openstack-extras'
    assert source_name('not-packaged', {}) is None


def test_vcs_git_keeps_debian_branch_hint():
    assert vcs_git('https://salsa.example/package.git -b debian/hibiscus') == (
        'https://salsa.example/package.git', 'debian/hibiscus')


def test_obsolete_archive_vcs_path_uses_reviewed_repository_override():
    item = archive('python-cyborgclient')
    item['Vcs-Git'] = 'https://salsa.debian.org/openstack-team/python/python-cyborgclient.git'
    record = package_record(
        'python-cyborgclient',
        {'repository-settings': {'openstack/python-cyborgclient': {}}},
        item, series='2026.2', membership='cycle')
    assert record['archive_packaging_repository'] == (
        'https://salsa.debian.org/openstack-team/clients/python-cyborgclient.git')


def test_branch_selection_uses_release_metadata():
    metadata = {'repository-settings': {'openstack/glance': {}}, 'branches': [{'name': 'stable/2026.2', 'location': '33.0.0.0rc1'}]}
    record = package_record('glance', metadata, archive('glance'), series='2026.2', membership='cycle')
    assert record['upstream_ref'] == 'stable/2026.2'
    assert record['archive_source']['sha256'] == 'a' * 64
    assert record['packaging_repository'].startswith(
        'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/')
    assert record['packaging_upstream_branch_candidates'][0] == 'upstream-hibiscus'
    assert record['packaging_pristine_tar_branch'] == 'pristine-tar'
    assert record['archive_source']['files'] == [{
        'name': 'glance_2.0-0ubuntu1.dsc',
        'url': 'https://archive.ubuntu.com/ubuntu/pool/main/p/glance/glance_2.0-0ubuntu1.dsc',
        'sha256': 'a' * 64,
        'size': 123,
    }]
    assert record['archive_version'].startswith('1:')


def test_multirepo_deliverable_is_not_silently_guessed():
    record = package_record('roles', {'repository-settings': {'openstack/role-a': {}, 'openstack/role-b': {}}}, archive('roles'), series='2026.2', membership='cycle')
    assert record['upstream_repository'] is None
    assert record['discovery_error']


def test_transitive_independent_dependencies_and_exclusions(tmp_path):
    root = tmp_path / 'releases'
    for series in ('hibiscus', '_independent'):
        (root / 'deliverables' / series).mkdir(parents=True)
    def release(series, name):
        (root / 'deliverables' / series / (name + '.yaml')).write_text(json.dumps({'repository-settings': {'openstack/' + name: {}}, 'type': 'library'}))
    release('hibiscus', 'service')
    release('hibiscus', 'unpackaged')
    for name in ('helper', 'helper2', 'unrelated'):
        release('_independent', name)
    rows = [archive('service', depends='python3-helper'), archive('python-helper', 'python3-helper', 'python3-helper2'), archive('python-helper2', 'python3-helper2', 'python3-helper'), archive('python-unrelated')]
    index = tmp_path / 'Sources'
    index.write_text('\n\n'.join('\n'.join(f'{key}: {value}' for key, value in row.items()) for row in rows))
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    result = make_catalog(root, [index])
    assert {p['source'] for p in result['packages']} == {'service', 'python-helper', 'python-helper2'}
    assert result['exclusions'][0]['deliverable'] == 'unpackaged'
    assert next(p for p in result['packages'] if p['source'] == 'service')['build_dependencies'] == ['python-helper']
    assert len(result['archive_indexes'][0]['sha256']) == 64


def test_continuation_fields():
    assert paragraphs('Package: sample\nBuild-Depends: python3-a,\n python3-b\n\n')[0]['Build-Depends'] == 'python3-a,\npython3-b'


def test_live_indexes_cover_release_updates_and_security():
    indexes = ubuntu_source_indexes('resolute')
    assert len(indexes) == 6
    assert indexes[0]['url'].endswith('/dists/resolute/main/source/Sources.xz')
    assert any('/dists/resolute-updates/universe/' in item['url'] for item in indexes)
    assert any(item['url'].startswith('https://security.ubuntu.com/ubuntu/dists/resolute-security/')
               for item in indexes)


def test_archive_preflight_checks_every_source_file_and_rejects_missing():
    requested = []

    class Response:
        headers = {'Content-Length': '123'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def opener(request, timeout):
        requested.append((request.full_url, request.method, timeout))
        if request.full_url.endswith('missing.tar.gz'):
            raise HTTPError(request.full_url, 404, 'missing', {}, None)
        return Response()

    data = {'packages': [{'source': 'example', 'archive_source': {
        'url': 'https://archive.example/example.dsc', 'sha256': 'a' * 64,
        'files': [
            {'url': 'https://archive.example/example.dsc', 'size': 123},
            {'url': 'https://archive.example/missing.tar.gz', 'size': 123},
        ],
    }}]}
    with pytest.raises(ValueError, match='missing.tar.gz'):
        validate_archive_sources(data, opener=opener, max_workers=1)
    assert requested == [
        ('https://archive.example/example.dsc', 'HEAD', 30),
        ('https://archive.example/missing.tar.gz', 'HEAD', 30),
    ]


def test_unrelated_name_collisions_are_rejected():
    sources = {
        'bifrost': {'Vcs-Git': 'https://salsa.debian.org/med-team/bifrost.git'},
        'trove': {'Vcs-Git': 'https://salsa.debian.org/java-team/trove.git'},
        'taskflow': {'Homepage': 'https://taskflow.github.io/', 'Vcs-Git': 'https://salsa.debian.org/debian/taskflow.git'},
        'python-taskflow': archive('python-taskflow'),
    }
    assert source_name('bifrost', sources) is None
    assert source_name('trove', sources) is None
    assert source_name('taskflow', sources) == 'python-taskflow'
    assert source_name('unknown', {'unknown': {}}) is None


def test_latest_archive_version_wins_independent_of_index_order(tmp_path):
    root = tmp_path / 'releases'
    (root / 'deliverables' / 'hibiscus').mkdir(parents=True)
    (root / 'deliverables' / 'hibiscus' / 'example.yaml').write_text(json.dumps({'repository-settings': {'openstack/example': {}}}))
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    indexes = []
    for index, version in enumerate(['1:2.0-0ubuntu2', '1:2.0-0ubuntu1', '1:2.0-0ubuntu1.1']):
        row = archive('example')
        row['Version'] = version
        path = tmp_path / f'Sources{index}'
        path.write_text('\n'.join(f'{key}: {value}' for key, value in row.items()))
        indexes.append(path)
    result = make_catalog(root, indexes)
    assert result['packages'][0]['archive_version'] == '1:2.0-0ubuntu2'
