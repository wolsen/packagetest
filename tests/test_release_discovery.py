import pytest

from packagetest.release_discovery import latest_release_from_deliverable_yaml


def test_release_discovery_uses_latest_release_block_metadata():
    content = """
releases:
  - version: 1.0.0
    projects:
      - repo: openstack/foo
        hash: 1111111111111111111111111111111111111111
  - version: 1.1.0
    projects:
      - repo: openstack/foo
        hash: 2222222222222222222222222222222222222222
"""
    release = latest_release_from_deliverable_yaml(content)
    assert release.version == "1.1.0"
    assert release.project_repo == "openstack/foo"
    assert release.project_hash == "2222222222222222222222222222222222222222"


def test_release_discovery_raises_without_release_entries():
    with pytest.raises(ValueError, match="No releases found"):
        latest_release_from_deliverable_yaml("team: sample\n")
