import pytest

from packagetest.models import PackageDefinition
from packagetest.release_discovery import (
    OpenStackReleaseResolver,
    ReleaseDiscoveryError,
    latest_release_from_deliverable_yaml,
    parse_series_status_yaml,
    resolve_release_from_deliverable_yaml,
)


SERIES_STATUS = """
- name: indri
  release-id: 2027.1
  status: future
- name: hibiscus
  release-id: 2026.2
  status: development
"""

SERIES_DELIVERABLE = """
releases:
  - version: 31.0.0.0b1
    projects:
      - repo: openstack/glance
        hash: 1111111111111111111111111111111111111111
  - version: 31.0.0.0rc1
    projects:
      - repo: openstack/glance
        hash: 2222222222222222222222222222222222222222
  - version: 31.0.0
    projects:
      - repo: openstack/glance
        hash: 3333333333333333333333333333333333333333
  - version: 31.1.0
    projects:
      - repo: openstack/glance
        hash: 4444444444444444444444444444444444444444
"""

INDEPENDENT_DELIVERABLE = """
releases:
  - version: 5.6.0
    projects:
      - repo: openstack/pbr
        hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  - version: 5.7.0
    projects:
      - repo: openstack/pbr
        hash: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
"""


def test_release_discovery_uses_latest_release_block_metadata():
    release = latest_release_from_deliverable_yaml(INDEPENDENT_DELIVERABLE)
    assert release.version == "5.7.0"
    assert release.project_repo == "openstack/pbr"
    assert release.project_hash == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_release_discovery_raises_without_release_entries():
    with pytest.raises(ValueError, match="No releases found"):
        latest_release_from_deliverable_yaml("team: sample\n")


def test_parse_series_status_yaml_reads_release_ids():
    series = parse_series_status_yaml(SERIES_STATUS)
    assert series[0].name == "indri"
    assert series[0].release_id == "2027.1"
    assert series[0].status == "future"


def test_resolve_release_from_deliverable_yaml_supports_stage_targets():
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1-b1", deliverable_scope="indri").version == "31.0.0.0b1"
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1-rc1", deliverable_scope="indri").version == "31.0.0.0rc1"
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1-final", deliverable_scope="indri").version == "31.0.0"
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1", deliverable_scope="indri").version == "31.1.0"


def test_resolver_uses_series_deliverables_and_snapshot_hashes():
    package = PackageDefinition(
        source_package="glance",
        binary_packages=["glance"],
        upstream_repo="https://opendev.org/openstack/glance",
        packaging_repo="https://example.invalid/glance",
    )

    def fetcher(url: str) -> str:
        if url.endswith("/data/series_status.yaml"):
            return SERIES_STATUS
        if url.endswith("/deliverables/indri/glance.yaml"):
            return SERIES_DELIVERABLE
        raise ReleaseDiscoveryError(f"unexpected url: {url}")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    release = resolver.resolve(package=package, openstack_target="2027.1-rc1", snapshot_at="2026-09-18T05:32:15+00:00")

    assert release.series == "indri"
    assert release.version == "31.0.0.0rc1"
    assert release.project_hash == "2222222222222222222222222222222222222222"
    assert release.upstream_ref == "2222222222222222222222222222222222222222"
    assert release.deliverable_path == "deliverables/indri/glance.yaml"


def test_resolver_falls_back_to_independent_deliverable():
    package = PackageDefinition(
        source_package="pbr",
        binary_packages=["python3-pbr"],
        upstream_repo="https://opendev.org/openstack/pbr",
        packaging_repo="https://example.invalid/pbr",
    )

    def fetcher(url: str) -> str:
        if url.endswith("/data/series_status.yaml"):
            return SERIES_STATUS
        if url.endswith("/deliverables/_independent/pbr.yaml"):
            return INDEPENDENT_DELIVERABLE
        raise ReleaseDiscoveryError(f"Failed to fetch {url}: HTTP 404")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    release = resolver.resolve(package=package, openstack_target="2027.1")

    assert release.version == "5.7.0"
    assert release.upstream_ref == "5.7.0"
    assert release.deliverable_path == "deliverables/_independent/pbr.yaml"


def test_resolver_rejects_invalid_snapshot_timestamp():
    package = PackageDefinition(
        source_package="glance",
        binary_packages=["glance"],
        upstream_repo="https://opendev.org/openstack/glance",
        packaging_repo="https://example.invalid/glance",
    )

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=lambda _: SERIES_STATUS)
    with pytest.raises(ReleaseDiscoveryError, match="Invalid snapshot timestamp"):
        resolver.resolve(package=package, openstack_target="2027.1", snapshot_at="not-a-date")
