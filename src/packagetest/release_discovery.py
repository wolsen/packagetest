from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .models import PackageDefinition

DEFAULT_OPENSTACK_RELEASES_BASE_URL = "https://raw.githubusercontent.com/openstack/releases/master"

_VERSION_RE = re.compile(r"^\s*-\s+version:\s+(.+?)\s*$")
_REPO_RE = re.compile(r"^\s*-\s+repo:\s+(.+?)\s*$")
_HASH_RE = re.compile(r"^\s+hash:\s+([0-9A-Fa-f]{7,40})\s*$")
_SERIES_NAME_RE = re.compile(r"^\s*-\s+name:\s+(.+?)\s*$")
_SERIES_RELEASE_ID_RE = re.compile(r"^\s+release-id:\s+(.+?)\s*$")
_SERIES_STATUS_RE = re.compile(r"^\s+status:\s+(.+?)\s*$")
_OPENSTACK_TARGET_RE = re.compile(r"^(?P<release_id>\d+\.\d+)(?:-(?P<stage>b\d+|rc\d+|final))?$")
_PRERELEASE_RE = re.compile(r"(b\d+|rc\d+)$", re.IGNORECASE)


class ReleaseDiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class OpenStackRelease:
    version: str
    project_repo: str | None
    project_hash: str | None


@dataclass(frozen=True)
class OpenStackSeries:
    name: str
    release_id: str | None
    status: str


@dataclass(frozen=True)
class ResolvedRelease:
    series: str | None
    release_id: str | None
    version: str
    project_repo: str | None
    project_hash: str | None
    upstream_ref: str
    snapshot_at: str | None
    deliverable_path: str


def read_text_from_url(url: str) -> str:
    try:
        with urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        raise ReleaseDiscoveryError(f"Failed to fetch {url}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise ReleaseDiscoveryError(f"Failed to fetch {url}: {exc.reason}") from exc


def parse_series_status_yaml(content: str) -> list[OpenStackSeries]:
    series: list[OpenStackSeries] = []
    name: str | None = None
    release_id: str | None = None
    status: str | None = None
    for line in content.splitlines():
        name_match = _SERIES_NAME_RE.match(line)
        if name_match:
            if name is not None:
                series.append(OpenStackSeries(name=name, release_id=release_id, status=status or "unknown"))
            name = name_match.group(1).strip()
            release_id = None
            status = None
            continue
        if name is None:
            continue
        release_id_match = _SERIES_RELEASE_ID_RE.match(line)
        if release_id_match:
            release_id = release_id_match.group(1).strip()
            continue
        status_match = _SERIES_STATUS_RE.match(line)
        if status_match and status is None:
            status = status_match.group(1).strip()
    if name is not None:
        series.append(OpenStackSeries(name=name, release_id=release_id, status=status or "unknown"))
    return series


def latest_release_from_deliverable_yaml(content: str) -> OpenStackRelease:
    releases = releases_from_deliverable_yaml(content)
    if not releases:
        raise ValueError("No releases found in deliverable YAML")
    return releases[-1]


def releases_from_deliverable_yaml(content: str) -> list[OpenStackRelease]:
    releases: list[OpenStackRelease] = []
    current: OpenStackRelease | None = None
    pending_project_repo: str | None = None
    for line in content.splitlines():
        m = _VERSION_RE.match(line)
        if m:
            if current is not None:
                releases.append(current)
            current = OpenStackRelease(version=m.group(1).strip(), project_repo=None, project_hash=None)
            pending_project_repo = None
            continue
        r = _REPO_RE.match(line)
        if r and current is not None:
            pending_project_repo = r.group(1).strip()
            continue
        h = _HASH_RE.match(line)
        if h and current is not None:
            if pending_project_repo and current.project_repo is None:
                current = OpenStackRelease(
                    version=current.version,
                    project_repo=pending_project_repo,
                    project_hash=h.group(1).strip(),
                )
            pending_project_repo = None
    if current is not None:
        releases.append(current)
    return releases


def resolve_series(series: list[OpenStackSeries], openstack_target: str) -> OpenStackSeries:
    target = parse_openstack_target(openstack_target)
    for entry in series:
        if entry.release_id == target.release_id or entry.name == target.release_id:
            return entry
    raise ReleaseDiscoveryError(f"Unknown OpenStack series target: {target.release_id}")


@dataclass(frozen=True)
class ParsedOpenStackTarget:
    release_id: str
    stage: str | None


def parse_openstack_target(openstack_target: str) -> ParsedOpenStackTarget:
    match = _OPENSTACK_TARGET_RE.fullmatch(openstack_target)
    if not match:
        raise ReleaseDiscoveryError(f"Unsupported OpenStack target: {openstack_target}")
    return ParsedOpenStackTarget(
        release_id=match.group("release_id"),
        stage=match.group("stage"),
    )


def resolve_release_from_deliverable_yaml(
    content: str,
    *,
    openstack_target: str,
    deliverable_scope: str,
) -> OpenStackRelease:
    releases = releases_from_deliverable_yaml(content)
    if not releases:
        raise ReleaseDiscoveryError("No releases found in deliverable YAML")
    if deliverable_scope == "_independent":
        return releases[-1]

    parsed_target = parse_openstack_target(openstack_target)
    if parsed_target.stage is None:
        return releases[-1]
    if parsed_target.stage == "final":
        for release in releases:
            if not _PRERELEASE_RE.search(release.version):
                return release
        raise ReleaseDiscoveryError(f"No final release found for target: {openstack_target}")
    for release in releases:
        if release.version.lower().endswith(parsed_target.stage.lower()):
            return release
    raise ReleaseDiscoveryError(f"No matching release found for target: {openstack_target}")


def validate_snapshot_at(snapshot_at: str | None) -> str | None:
    if snapshot_at is None:
        return None
    try:
        datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseDiscoveryError(f"Invalid snapshot timestamp: {snapshot_at}") from exc
    return snapshot_at


def derive_deliverable_name(package: PackageDefinition) -> str:
    if package.openstack_deliverable:
        return package.openstack_deliverable
    upstream_repo = package.upstream_repo.rstrip("/")
    return upstream_repo.rsplit("/", 1)[-1]


class OpenStackReleaseResolver:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENSTACK_RELEASES_BASE_URL,
        fetcher: callable = read_text_from_url,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher
        self._series_status: list[OpenStackSeries] | None = None
        self._deliverable_cache: dict[str, str] = {}

    def resolve(self, *, package: PackageDefinition, openstack_target: str, snapshot_at: str | None = None) -> ResolvedRelease:
        snapshot_at = validate_snapshot_at(snapshot_at)
        deliverable_name = derive_deliverable_name(package)
        series = self._series_status_entries()
        resolved_series = resolve_series(series, openstack_target)
        deliverable_path, content = self._fetch_deliverable(resolved_series.name, deliverable_name)
        release = resolve_release_from_deliverable_yaml(
            content,
            openstack_target=openstack_target,
            deliverable_scope=deliverable_path.split("/")[-2],
        )
        upstream_ref = release.project_hash if snapshot_at and release.project_hash else release.version
        return ResolvedRelease(
            series=resolved_series.name,
            release_id=resolved_series.release_id,
            version=release.version,
            project_repo=release.project_repo,
            project_hash=release.project_hash,
            upstream_ref=upstream_ref,
            snapshot_at=snapshot_at,
            deliverable_path=deliverable_path,
        )

    def _series_status_entries(self) -> list[OpenStackSeries]:
        if self._series_status is None:
            content = self.fetcher(f"{self.base_url}/data/series_status.yaml")
            self._series_status = parse_series_status_yaml(content)
        return self._series_status

    def _fetch_deliverable(self, series_name: str, deliverable_name: str) -> tuple[str, str]:
        for scope in (series_name, "_independent"):
            path = f"deliverables/{scope}/{deliverable_name}.yaml"
            if path in self._deliverable_cache:
                return path, self._deliverable_cache[path]
            try:
                content = self.fetcher(f"{self.base_url}/{path}")
            except ReleaseDiscoveryError:
                continue
            self._deliverable_cache[path] = content
            return path, content
        raise ReleaseDiscoveryError(f"Deliverable not found for {deliverable_name} in series {series_name}")
