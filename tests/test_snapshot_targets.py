import json
from pathlib import Path

import pytest

from packagetest.snapshot_targets import (build_target, load_targets,
                                           uca_lts_for_openstack,
                                           ubuntu_release_for_openstack)


@pytest.mark.parametrize('series,ubuntu,uca', [
    ('2024.2', '24.10', '24.04'),
    ('2025.1', '25.04', '24.04'),
    ('2025.2', '25.10', '24.04'),
    ('2026.1', '26.04', '24.04'),
    ('2026.2', '26.10', '26.04'),
    ('2027.1', '27.04', '26.04'),
    ('2027.2', '27.10', '26.04'),
    ('2028.1', '28.04', '26.04'),
    ('2028.2', '28.10', '28.04'),
])
def test_release_cadence_derives_ubuntu_and_uca_baselines(series, ubuntu, uca):
    assert ubuntu_release_for_openstack(series) == ubuntu
    assert uca_lts_for_openstack(series) == uca


def test_hibiscus_targets_are_derived_from_release_mappings():
    path = Path(__file__).resolve().parents[1] / 'config/snapshot-targets.json'
    targets = load_targets(path)
    assert set(targets) == {'hibiscus-stonking', 'hibiscus-resolute-uca'}
    assert targets['hibiscus-stonking']['ubuntu_release'] == '26.10'
    assert targets['hibiscus-resolute-uca']['ubuntu_release'] == '26.04'
    assert targets['hibiscus-resolute-uca']['cloud_archive']['distribution'] == (
        'resolute-updates/hibiscus')
    assert build_target(targets['hibiscus-resolute-uca'])['chroot'] == (
        'resolute-uca-hibiscus-amd64-sbuild')


def test_indri_requires_only_ubuntu_2704_codename_mapping(tmp_path):
    config = {
        'ubuntu_suites': {'26.04': 'resolute', '27.04': 'future-codename'},
        'openstack_releases': {'2027.1': 'indri'},
    }
    path = tmp_path / 'targets.json'
    path.write_text(json.dumps(config))
    targets = load_targets(path)
    assert targets['indri-future-codename']['ubuntu_release'] == '27.04'
    assert targets['indri-resolute-uca']['ubuntu_release'] == '26.04'

