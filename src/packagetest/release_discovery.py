from __future__ import annotations

import json
import re
from threading import Lock
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener

from .models import PackageDefinition

DEFAULT_OPENSTACK_RELEASES_BASE_URL = "https://raw.githubusercontent.com/openstack/releases/master"
_VERSION_RE = re.compile(r"^\s*-\s+version:\s+(.+?)\s*$")
_REPO_RE = re.compile(r"^\s*-\s+repo:\s+(.+?)\s*$")
_HASH_RE = re.compile(r"^\s+hash:\s+([0-9A-Fa-f]{7,40})\s*$")
_SERIES_NAME_RE = re.compile(r"^\s*-\s+name:\s+(.+?)\s*$")
_SERIES_RELEASE_ID_RE = re.compile(r"^\s+release-id:\s+(.+?)\s*$")
_SERIES_STATUS_RE = re.compile(r"^\s+status:\s+(.+?)\s*$")
_BRANCH_NAME_RE = re.compile(r"^\s*-\s+name:\s+(.+?)\s*$")
_BRANCH_LOCATION_RE = re.compile(r"^\s+location:\s+(.+?)\s*$")
_OPENSTACK_TARGET_RE = re.compile(r"^(?P<release_id>(?:\d+\.\d+|[a-z][a-z0-9-]*))(?:-(?P<stage>b\d+|rc\d+|final))?$", re.IGNORECASE)
_PRERELEASE_RE = re.compile(r"(b\d+|rc\d+)$", re.IGNORECASE)


class ReleaseDiscoveryError(ValueError):
    pass


class ReleaseNotFoundError(ReleaseDiscoveryError):
    pass


class HTTPTextClient:
    def __init__(self, *, timeout: int = 30, retries: int = 2, user_agent: str = "packagetest/0.1") -> None:
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.opener = build_opener()

    def fetch(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.user_agent})
        last_error: URLError | None = None
        for _ in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8")
            except HTTPError as exc:
                if exc.code == 404:
                    raise ReleaseNotFoundError(f"Failed to fetch {url}: HTTP 404") from exc
                raise ReleaseDiscoveryError(f"Failed to fetch {url}: HTTP {exc.code}") from exc
            except URLError as exc:
                last_error = exc
        reason = last_error.reason if last_error is not None else "unknown error"
        raise ReleaseDiscoveryError(f"Failed to fetch {url}: {reason}")


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


@dataclass(frozen=True)
class ParsedOpenStackTarget:
    release_id: str
    stage: str | None


def read_text_from_url(url: str) -> str:
    return HTTPTextClient().fetch(url)


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
        version_match = _VERSION_RE.match(line)
        if version_match:
            if current is not None:
                releases.append(current)
            current = OpenStackRelease(version=version_match.group(1).strip(), project_repo=None, project_hash=None)
            pending_project_repo = None
            continue
        repo_match = _REPO_RE.match(line)
        if repo_match and current is not None:
            pending_project_repo = repo_match.group(1).strip()
            continue
        hash_match = _HASH_RE.match(line)
        if hash_match and current is not None:
            if pending_project_repo and current.project_repo is None:
                current = OpenStackRelease(
                    version=current.version,
                    project_repo=pending_project_repo,
                    project_hash=hash_match.group(1).strip(),
                )
            pending_project_repo = None
    if current is not None:
        releases.append(current)
    return releases


def parse_openstack_target(openstack_target: str) -> ParsedOpenStackTarget:
    match = _OPENSTACK_TARGET_RE.fullmatch(openstack_target)
    if not match:
        raise ReleaseDiscoveryError(f"Unsupported OpenStack target: {openstack_target}")
    return ParsedOpenStackTarget(release_id=match.group("release_id").lower(), stage=match.group("stage"))


def resolve_series(series: list[OpenStackSeries], openstack_target: str) -> OpenStackSeries:
    target = parse_openstack_target(openstack_target)
    for entry in series:
        if (entry.release_id and entry.release_id.lower() == target.release_id) or entry.name.lower() == target.release_id:
            return entry
    raise ReleaseDiscoveryError(f"Unknown OpenStack series target: {target.release_id}")


def branch_locations_from_deliverable_yaml(content: str) -> dict[str, str]:
    branches: dict[str, str] = {}
    in_branches = False
    current_branch: str | None = None
    for line in content.splitlines():
        if line.startswith("branches:"):
            in_branches = True
            current_branch = None
            continue
        if not in_branches:
            continue
        if line and not line.startswith(" ") and not line.startswith("-"):
            break
        branch_match = _BRANCH_NAME_RE.match(line)
        if branch_match:
            current_branch = branch_match.group(1).strip()
            continue
        location_match = _BRANCH_LOCATION_RE.match(line)
        if location_match and current_branch is not None:
            branches[current_branch] = location_match.group(1).strip()
    return branches


def _release_index_by_version(releases: list[OpenStackRelease]) -> dict[str, int]:
    return {release.version: index for index, release in enumerate(releases)}


def _candidate_releases_for_series(content: str, releases: list[OpenStackRelease], release_id: str) -> list[OpenStackRelease]:
    branch_locations = branch_locations_from_deliverable_yaml(content)
    version_indexes = _release_index_by_version(releases)
    stable_branch_indexes = sorted(
        (version_indexes[location], branch_name)
        for branch_name, location in branch_locations.items()
        if branch_name.startswith("stable/") and location in version_indexes
    )
    start_index = 0
    end_index = len(releases)
    current_branch = f"stable/{release_id}"
    for position, (index, branch_name) in enumerate(stable_branch_indexes):
        if branch_name != current_branch:
            continue
        start_index = stable_branch_indexes[position - 1][0] + 1 if position > 0 else 0
        end_index = stable_branch_indexes[position + 1][0] if position + 1 < len(stable_branch_indexes) else len(releases)
        break
    return releases[start_index:end_index]


def _numeric_series_prefix(version: str) -> str | None:
    match = re.match(r"^(?P<prefix>\d+)\.", version)
    if match:
        return match.group("prefix")
    return None


def resolve_release_from_deliverable_yaml(
    content: str,
    *,
    openstack_target: str,
    deliverable_scope: str,
) -> OpenStackRelease:
    releases = releases_from_deliverable_yaml(content)
    if not releases:
        raise ReleaseDiscoveryError("No releases found in deliverable YAML")
    parsed_target = parse_openstack_target(openstack_target)
    if deliverable_scope == "_independent":
        if parsed_target.stage is not None:
            for release in releases:
                if release.version.lower().endswith(parsed_target.stage.lower()):
                    return release
        return releases[-1]

    candidate_releases = _candidate_releases_for_series(content, releases, parsed_target.release_id)
    stable_releases = [release for release in candidate_releases if not _PRERELEASE_RE.search(release.version)]
    if parsed_target.stage is None or parsed_target.stage == "final":
        if stable_releases:
            return stable_releases[-1]
        raise ReleaseDiscoveryError(f"No stable release found for target: {openstack_target}")
    branch_locations = branch_locations_from_deliverable_yaml(content)
    if f"stable/{parsed_target.release_id}" not in branch_locations:
        stable_prefixes = {
            prefix for prefix in (_numeric_series_prefix(release.version) for release in stable_releases) if prefix is not None
        }
        if len(stable_prefixes) > 1:
            raise ReleaseDiscoveryError(f"Cannot safely resolve staged target without stable branch metadata: {openstack_target}")
    if stable_releases:
        target_prefix = _numeric_series_prefix(stable_releases[-1].version)
        if target_prefix is not None:
            candidate_releases = [
                release for release in candidate_releases if _numeric_series_prefix(release.version) == target_prefix
            ]
    for release in candidate_releases:
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
        fetcher: Callable[[str], str] = read_text_from_url,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher
        self._series_status: list[OpenStackSeries] | None = None
        self._deliverable_cache: dict[str, str] = {}
        self._repo_metadata_cache: dict[str, dict] = {}
        self._resolved_cache: dict[tuple[str, str, str, str, str | None], ResolvedRelease] = {}
        self._cache_lock = Lock()

    def resolve(self, *, package: PackageDefinition, openstack_target: str, snapshot_at: str | None = None) -> ResolvedRelease:
        snapshot_at = validate_snapshot_at(snapshot_at)
        deliverable_name = derive_deliverable_name(package)
        cache_key = (package.source_package, package.upstream_repo, deliverable_name, openstack_target, snapshot_at)
        with self._cache_lock:
            cached = self._resolved_cache.get(cache_key)
        if cached is not None:
            return cached
        resolved_series = resolve_series(self._series_status_entries(), openstack_target)
        deliverable_path, content = self._fetch_deliverable(resolved_series.name, deliverable_name)
        deliverable_target = resolved_series.release_id or openstack_target
        if deliverable_path.split("/")[-2] != "_independent":
            deliverable_target = openstack_target
        release = resolve_release_from_deliverable_yaml(
            content,
            openstack_target=deliverable_target,
            deliverable_scope=deliverable_path.split("/")[-2],
        )
        upstream_ref = release.version
        if snapshot_at:
            if release.project_repo is None:
                raise ReleaseDiscoveryError(f"Snapshot resolution requires a project repo for {deliverable_name}")
            upstream_ref = self._resolve_snapshot_ref(
                upstream_repo=package.upstream_repo,
                project_repo=release.project_repo,
                release_id=resolved_series.release_id,
                deliverable_scope=deliverable_path.split("/")[-2],
                snapshot_at=snapshot_at,
                deliverable_content=content,
            )
        resolved_release = ResolvedRelease(
            series=resolved_series.name,
            release_id=resolved_series.release_id,
            version=release.version,
            project_repo=release.project_repo,
            project_hash=upstream_ref if snapshot_at else release.project_hash,
            upstream_ref=upstream_ref,
            snapshot_at=snapshot_at,
            deliverable_path=deliverable_path,
        )
        with self._cache_lock:
            self._resolved_cache[cache_key] = resolved_release
        return resolved_release

    def _series_status_entries(self) -> list[OpenStackSeries]:
        if self._series_status is None:
            parsed = parse_series_status_yaml(self.fetcher(f"{self.base_url}/data/series_status.yaml"))
            with self._cache_lock:
                if self._series_status is None:
                    self._series_status = parsed
        return self._series_status

    def _fetch_deliverable(self, series_name: str, deliverable_name: str) -> tuple[str, str]:
        for scope in (series_name, "_independent"):
            path = f"deliverables/{scope}/{deliverable_name}.yaml"
            if path in self._deliverable_cache:
                return path, self._deliverable_cache[path]
            try:
                content = self.fetcher(f"{self.base_url}/{path}")
            except ReleaseNotFoundError:
                continue
            with self._cache_lock:
                self._deliverable_cache[path] = content
            return path, content
        raise ReleaseDiscoveryError(f"Deliverable not found for {deliverable_name} in series {series_name}")

    def _resolve_snapshot_ref(
        self,
        *,
        upstream_repo: str,
        project_repo: str,
        release_id: str | None,
        deliverable_scope: str,
        snapshot_at: str,
        deliverable_content: str,
    ) -> str:
        branch = self._default_branch_for_upstream_repo(upstream_repo)
        if deliverable_scope != "_independent" and release_id is not None:
            branch_locations = branch_locations_from_deliverable_yaml(deliverable_content)
            if f"stable/{release_id}" in branch_locations:
                branch = f"stable/{release_id}"
        query = urlencode({"sha": branch, "until": snapshot_at, "limit": 1})
        payload = self.fetcher(_canonical_commit_api_url(upstream_repo, query))
        try:
            commits = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ReleaseDiscoveryError(f"Invalid commit API response for {project_repo} at {snapshot_at}") from exc
        if not commits:
            raise ReleaseDiscoveryError(f"No upstream commit found for {project_repo} at {snapshot_at}")
        sha = commits[0].get("sha")
        if not sha:
            raise ReleaseDiscoveryError(f"Snapshot response did not include a commit SHA for {project_repo}")
        return sha

    def _default_branch_for_upstream_repo(self, upstream_repo: str) -> str:
        with self._cache_lock:
            cached = self._repo_metadata_cache.get(upstream_repo)
        if cached is None:
            payload = self.fetcher(_canonical_repo_api_url(upstream_repo))
            try:
                cached = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ReleaseDiscoveryError(f"Invalid repository API response for {upstream_repo}") from exc
            with self._cache_lock:
                self._repo_metadata_cache[upstream_repo] = cached
        default_branch = cached.get("default_branch")
        if not default_branch:
            raise ReleaseDiscoveryError(f"Repository API response did not include a default branch for {upstream_repo}")
        return default_branch


def _canonical_commit_api_url(upstream_repo: str, query: str) -> str:
    parsed = urlparse(upstream_repo)
    if not parsed.scheme or not parsed.netloc or not parsed.path:
        raise ReleaseDiscoveryError(f"Unsupported upstream repository URL: {upstream_repo}")
    repo_path = parsed.path.strip("/")
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/repos/{repo_path}/commits?{query}"


def _canonical_repo_api_url(upstream_repo: str) -> str:
    parsed = urlparse(upstream_repo)
    if not parsed.scheme or not parsed.netloc or not parsed.path:
        raise ReleaseDiscoveryError(f"Unsupported upstream repository URL: {upstream_repo}")
    repo_path = parsed.path.strip("/")
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/repos/{repo_path}"
