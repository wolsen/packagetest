"""Verify target isolation, binary publication evidence, and shared guest setup."""
import gzip
import io
import subprocess

import pytest

from packagetest.autopkgtest import _testbed_setup
from packagetest.catalog import populate_binary_catalog
from packagetest.prepared_plan import archive_versions
from packagetest.repositories import (PPA_FINGERPRINT, binary_indexes, profile_target,
                                      repository_line, repository_setup, target_repositories)


@pytest.mark.parametrize('profile', ['stonking', 'resolute', 'resolute-uca-hibiscus'])
def test_target_repositories_are_shared_with_guest_setup(profile, tmp_path):
    """Distro guests never inherit UCA, and UCA guests include every configured pocket."""
    target = profile_target(profile)
    repos = target_repositories(target)
    script = repository_setup(target)
    assert [r['distribution'] for r in repos[:4]] == [
        target['suite'] + suffix for suffix in ('', '-updates', '-security', '-proposed')]
    for repo in repos:
        assert repository_line(repo) in script
        assert f"Pin: release n={repo['distribution']}\nPin-Priority: 500" in script

    if '-uca-' in profile:
        assert len(repos) == 8
        assert PPA_FINGERPRINT in script
        assert {r['repository'].split('/')[-2] for r in repos[-3:]} == {
            'hibiscus-updates', 'hibiscus-staging', 'hibiscus-proposed'}
    else:
        assert len(repos) == 4
        assert 'launchpad' not in script
        assert 'cloud' not in script

    setup = _testbed_setup(tmp_path, target).read_text()
    # Target setup replaces inherited source files, so local candidates must be added afterward.
    assert setup.index('packagetest-target.list') < setup.index('file:/opt/packagetest-candidate')
    assert 'cp /opt/packagetest-candidate/preferences' in setup
    subprocess.run(['sh', '-n'], input=setup, text=True, check=True)


def test_binary_catalog_uses_published_versions_and_debian_order(tmp_path):
    """Staging may satisfy the failed run, but proposed wins when its Debian version is newer."""
    target = profile_target('resolute-uca-hibiscus')
    catalog = {'target': target, 'packages': [{'source': 'python-glance-store',
                'binaries': ['python3-glance-store'], 'archive_version': '99.0-source-only'}]}
    requested = []

    def opener(request, **kwargs):
        """Supply compact binary indexes for each enabled archive without network access."""
        url = request.full_url
        requested.append(url)
        version = '5.2.0-0ubuntu1'
        if 'hibiscus-staging' in url:
            version = '5.7.0-0ubuntu1~cloud0'
        elif 'hibiscus-proposed' in url:
            version = '1:5.3.0-0ubuntu1~cloud0'

        text = (f'Package: python3-glance-store\nSource: python-glance-store (5.2.0)\n'
                f'Architecture: all\nVersion: {version}\n\n')
        return io.BytesIO(gzip.compress(text.encode()))

    populate_binary_catalog(catalog, tmp_path, opener=opener)
    assert requested == [item['url'] for item in binary_indexes(target)]
    assert catalog['archive_python_packages'][0]['version'] == '1:5.3.0-0ubuntu1~cloud0'
    assert archive_versions(catalog)['python3-glance-store'] == [
        {'source': 'python-glance-store', 'version': '1:5.3.0-0ubuntu1~cloud0'}]
    assert all(len(item['sha256']) == 64 for item in catalog['archive_binary_indexes'])


def test_empty_binary_indexes_cannot_prove_source_only_package_availability(tmp_path):
    """A source stanza must not fabricate a binary that has not been published."""
    catalog = {'suite': 'resolute', 'packages': [{'source': 'python-example',
               'binaries': ['python3-example'], 'archive_version': '99.0'}]}
    populate_binary_catalog(catalog, tmp_path, opener=lambda *args, **kwargs: io.BytesIO(gzip.compress(b'')))
    assert archive_versions(catalog) == {}
    assert catalog['archive_python_packages'] == []
