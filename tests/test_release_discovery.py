import pytest
from urllib.error import URLError

from packagetest.models import PackageDefinition
from packagetest.release_discovery import (
    HTTPTextClient,
    OpenStackReleaseResolver,
    ReleaseDiscoveryError,
    ReleaseNotFoundError,
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


def test_resolver_accepts_series_name_target():
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
    release = resolver.resolve(package=package, openstack_target="indri")
    assert release.series == "indri"
    assert release.version == "31.1.0"


def test_resolve_release_from_deliverable_yaml_supports_stage_targets():
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1-b1", deliverable_scope="indri").version == "31.0.0.0b1"
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1-rc1", deliverable_scope="indri").version == "31.0.0.0rc1"
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1-final", deliverable_scope="indri").version == "31.1.0"
    assert resolve_release_from_deliverable_yaml(SERIES_DELIVERABLE, openstack_target="2027.1", deliverable_scope="indri").version == "31.1.0"


def test_independent_deliverable_accepts_stage_specific_target_fallback():
    release = resolve_release_from_deliverable_yaml(
        INDEPENDENT_DELIVERABLE,
        openstack_target="2027.1-b1",
        deliverable_scope="_independent",
    )
    assert release.version == "5.7.0"


def test_independent_deliverable_prefers_matching_stage_when_available():
    content = """
releases:
  - version: 5.7.0b1
    projects:
      - repo: openstack/pbr
        hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  - version: 5.7.0
    projects:
      - repo: openstack/pbr
        hash: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
"""
    release = resolve_release_from_deliverable_yaml(
        content,
        openstack_target="2027.1-b1",
        deliverable_scope="_independent",
    )
    assert release.version == "5.7.0b1"


def test_plain_cycle_target_ignores_trailing_prerelease():
    content = SERIES_DELIVERABLE + """
  - version: 31.2.0.0rc1
    projects:
      - repo: openstack/glance
        hash: 5555555555555555555555555555555555555555
"""
    assert resolve_release_from_deliverable_yaml(content, openstack_target="2027.1", deliverable_scope="indri").version == "31.1.0"


def test_plain_cycle_target_stops_before_next_series_branch():
    content = SERIES_DELIVERABLE + """
  - version: 32.0.0.0b1
    projects:
      - repo: openstack/glance
        hash: 5555555555555555555555555555555555555555
  - version: 32.0.0
    projects:
      - repo: openstack/glance
        hash: 6666666666666666666666666666666666666666
branches:
  - name: stable/2027.1
    location: 31.0.0.0rc1
  - name: stable/2027.2
    location: 32.0.0.0b1
"""
    assert resolve_release_from_deliverable_yaml(content, openstack_target="2027.1", deliverable_scope="indri").version == "31.1.0"


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
            return SERIES_DELIVERABLE + """
branches:
  - name: stable/2027.1
    location: 31.0.0.0rc1
"""
        if url.endswith("/api/v1/repos/openstack/glance"):
            return '{"default_branch":"main"}'
        if "opendev.org/api/v1/repos/openstack/glance/commits" in url:
            return '[{"sha":"snapshotsha"}]'
        raise ReleaseDiscoveryError(f"unexpected url: {url}")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    release = resolver.resolve(package=package, openstack_target="2027.1-rc1", snapshot_at="2026-09-18T05:32:15+00:00")

    assert release.series == "indri"
    assert release.version == "31.0.0.0rc1"
    assert release.project_hash == "snapshotsha"
    assert release.upstream_ref == "snapshotsha"
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
        raise ReleaseNotFoundError(f"Failed to fetch {url}: HTTP 404")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    release = resolver.resolve(package=package, openstack_target="2027.1")

    assert release.version == "5.7.0"
    assert release.upstream_ref == "5.7.0"
    assert release.deliverable_path == "deliverables/_independent/pbr.yaml"


def test_resolver_caches_release_by_deliverable_target_and_snapshot():
    package = PackageDefinition(
        source_package="glance",
        binary_packages=["glance"],
        upstream_repo="https://opendev.org/openstack/glance",
        packaging_repo="https://example.invalid/glance",
    )
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        if url.endswith("/data/series_status.yaml"):
            return SERIES_STATUS
        if url.endswith("/deliverables/indri/glance.yaml"):
            return SERIES_DELIVERABLE
        raise ReleaseDiscoveryError(f"unexpected url: {url}")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    first = resolver.resolve(package=package, openstack_target="2027.1")
    second = resolver.resolve(package=package, openstack_target="2027.1")

    assert first == second
    assert calls.count("https://example.invalid/data/series_status.yaml") == 1
    assert calls.count("https://example.invalid/deliverables/indri/glance.yaml") == 1


def test_resolver_cache_distinguishes_packages_sharing_deliverable():
    first_package = PackageDefinition(
        source_package="glance-a",
        binary_packages=["glance"],
        upstream_repo="https://opendev.org/openstack/glance",
        packaging_repo="https://example.invalid/glance-a",
        openstack_deliverable="glance",
    )
    second_package = PackageDefinition(
        source_package="glance-b",
        binary_packages=["glance"],
        upstream_repo="https://opendev.org/openstack/glance",
        packaging_repo="https://example.invalid/glance-b",
        openstack_deliverable="glance",
    )
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        if url.endswith("/data/series_status.yaml"):
            return SERIES_STATUS
        if url.endswith("/deliverables/indri/glance.yaml"):
            return SERIES_DELIVERABLE
        raise ReleaseDiscoveryError(f"unexpected url: {url}")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    first = resolver.resolve(package=first_package, openstack_target="2027.1")
    second = resolver.resolve(package=second_package, openstack_target="2027.1")

    assert first == second
    assert calls.count("https://example.invalid/deliverables/indri/glance.yaml") == 1


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


def test_snapshot_resolution_requires_snapshot_commit():
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
        if url.endswith("/api/v1/repos/openstack/glance"):
            return '{"default_branch":"main"}'
        if "opendev.org/api/v1/repos/openstack/glance/commits" in url:
            return "[]"
        raise ReleaseDiscoveryError(f"unexpected url: {url}")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    with pytest.raises(ReleaseDiscoveryError, match="No upstream commit found"):
        resolver.resolve(package=package, openstack_target="2027.1", snapshot_at="2026-09-18T05:32:15+00:00")


def test_http_text_client_retries_plus_initial_attempt(monkeypatch):
    attempts = {"count": 0}

    class FakeOpener:
        def open(self, request, timeout):
            attempts["count"] += 1
            raise URLError("temporary")

    client = HTTPTextClient(retries=2)
    client.opener = FakeOpener()

    with pytest.raises(ReleaseDiscoveryError, match="temporary"):
        client.fetch("https://example.invalid/data/series_status.yaml")
    assert attempts["count"] == 3


def test_snapshot_resolution_wraps_invalid_commit_api_payload():
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
        if url.endswith("/api/v1/repos/openstack/glance"):
            return '{"default_branch":"main"}'
        if "opendev.org/api/v1/repos/openstack/glance/commits" in url:
            return "not-json"
        raise ReleaseDiscoveryError(f"unexpected url: {url}")

    resolver = OpenStackReleaseResolver(base_url="https://example.invalid", fetcher=fetcher)
    with pytest.raises(ReleaseDiscoveryError, match="Invalid commit API response"):
        resolver.resolve(package=package, openstack_target="2027.1", snapshot_at="2026-09-18T05:32:15+00:00")
