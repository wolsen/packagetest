from unittest.mock import Mock, patch

from packagetest.versioning import debian_compare, openstack_target_to_debian_version, openstack_target_to_upstream_version


def _completed(code: int):
    obj = Mock()
    obj.returncode = code
    obj.stdout = ""
    obj.stderr = ""
    return obj


@patch("packagetest.versioning.subprocess.run")
def test_debian_compare_lt(mock_run):
    # first call lt succeeds
    mock_run.side_effect = [_completed(0)]
    assert debian_compare("1.0~rc1", "1.0") == -1


@patch("packagetest.versioning.subprocess.run")
def test_debian_compare_gt(mock_run):
    # lt fails, gt succeeds
    mock_run.side_effect = [_completed(1), _completed(0)]
    assert debian_compare("2:1.0", "1:9.0") == 1


@patch("packagetest.versioning.subprocess.run")
def test_debian_compare_eq(mock_run):
    mock_run.side_effect = [_completed(1), _completed(1)]
    assert debian_compare("1.0-1", "1.0-1") == 0


def test_openstack_target_to_upstream_version_maps_pre_releases():
    assert openstack_target_to_upstream_version("2027.1-b1") == "2027.1~b1"
    assert openstack_target_to_upstream_version("2027.1-rc1") == "2027.1~rc1"
    assert openstack_target_to_upstream_version("2027.1-final") == "2027.1"
    assert openstack_target_to_upstream_version("2027.1") == "2027.1"


def test_openstack_target_to_debian_version_appends_ubuntu_revision():
    assert openstack_target_to_debian_version("2027.1-b1") == "2027.1~b1-0ubuntu1"
    assert openstack_target_to_debian_version("2027.1-final") == "2027.1-0ubuntu1"
