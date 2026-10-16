"""Resolve OpenStack release metadata and immutable snapshot references."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener

from .models import PackageDefinition

DEFAULT_OPENSTACK_RELEASES_BASE_URL = (
    "https://raw.githubusercontent.com/openstack/releases/master"
)
_VERSION_RE = re.compile(r"^\s*-\s+version:\s+(.+?)\s*$")
_REPO_RE = re.compile(r"^\s*-\s+repo:\s+(.+?)\s*$")
_HASH_RE = re.compile(r"^\s+hash:\s+([0-9A-Fa-f]{7,40})\s*$")
_SERIES_NAME_RE = re.compile(r"^\s*-\s+name:\s+(.+?)\s*$")
_SERIES_RELEASE_ID_RE = re.compile(r"^\s+release-id:\s+(.+?)\s*$")
_SERIES_STATUS_RE = re.compile(r"^\s+status:\s+(.+?)\s*$")
_BRANCH_NAME_RE = re.compile(r"^\s*-\s+name:\s+(.+?)\s*$")
_BRANCH_LOCATION_RE = re.compile(r"^\s+location:\s+(.+?)\s*$")
_OPENSTACK_TARGET_RE = re.compile(
    r"^(?P<release_id>(?:\d+\.\d+|[a-z][a-z0-9-]*))(?:-(?P<stage>b\d+|rc\d+|final))?$",
    re.IGNORECASE,
)
_PRERELEASE_RE = re.compile(r"(b\d+|rc\d+)$", re.IGNORECASE)


class ReleaseDiscoveryError(ValueError):
    """Report unavailable or malformed upstream release-discovery evidence."""

    pass


class ReleaseNotFoundError(ReleaseDiscoveryError):
    """Identify a definitive missing metadata resource that permits discovery fallback."""

    pass


class HTTPTextClient:
    """Fetch release metadata with bounded retries for transport failures."""

    def __init__(
        self,
        *,
        timeout: int = 30,
        retries: int = 2,
        user_agent: str = "packagetest/0.1",
    ) -> None:
        """Initialize metadata retrieval configuration and owned state."""
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.opener = build_opener()

    def fetch(self, url: str) -> str:
        """Retry transport failures and return decoded metadata or a contextual error."""
        request = Request(url, headers={"User-Agent": self.user_agent})
        last_error: URLError | None = None
        for _ in range(self.retries + 1):
            try:
                return self._fetch_once(request, url)
            except URLError as exc:
                last_error = exc

        reason = last_error.reason if last_error is not None else "unknown error"
        raise ReleaseDiscoveryError(f"Failed to fetch {url}: {reason}")

    def _fetch_once(self, request: Request, url: str) -> str:
        """Translate HTTP failures without retrying a definitive server response."""
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 404:
                raise ReleaseNotFoundError(f"Failed to fetch {url}: HTTP 404") from exc

            raise ReleaseDiscoveryError(
                f"Failed to fetch {url}: HTTP {exc.code}"
            ) from exc


@dataclass(frozen=True)
class OpenStackRelease:
    """Bind one declared release version to its optional project repository and commit."""

    version: str
    project_repo: str | None
    project_hash: str | None


@dataclass(frozen=True)
class OpenStackSeries:
    """Describe an OpenStack series and its declared release identity and support status."""

    name: str
    release_id: str | None
    status: str


@dataclass(frozen=True)
class ResolvedRelease:
    """Capture the selected release and immutable upstream reference for one package."""

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
    """Separate a requested release identity from its optional prerelease stage."""

    release_id: str
    stage: str | None


def read_text_from_url(url: str) -> str:
    """Fetch release metadata using the bounded HTTP client policy."""
    return HTTPTextClient().fetch(url)


class SeriesStatusParser:
    """Accumulate the first status and latest release ID for each named series."""

    def __init__(self) -> None:
        """Start without an active series or any completed entries."""
        self.series: list[OpenStackSeries] = []
        self.name: str | None = None
        self.release_id: str | None = None
        self.status: str | None = None

    def finish_entry(self) -> None:
        """Append the active series, retaining the unknown-status fallback."""
        if self.name is not None:
            self.series.append(
                OpenStackSeries(self.name, self.release_id, self.status or "unknown")
            )

    def consume(self, line: str) -> None:
        """Start a named series or consume one field of the current entry."""
        match = _SERIES_NAME_RE.match(line)
        if match:
            self.finish_entry()
            self.name = match.group(1).strip()
            self.release_id = None
            self.status = None
            return

        self._field(line)

    def _field(self, line: str) -> None:
        """Ignore fields before an entry and keep its first status declaration."""
        if self.name is None:
            return

        match = _SERIES_RELEASE_ID_RE.match(line)
        if match:
            self.release_id = match.group(1).strip()
            return

        match = _SERIES_STATUS_RE.match(line)
        if match and self.status is None:
            self.status = match.group(1).strip()


def parse_series_status_yaml(content: str) -> list[OpenStackSeries]:
    """Read series declarations in their metadata order."""
    parser = SeriesStatusParser()
    for line in content.splitlines():
        parser.consume(line)

    parser.finish_entry()
    return parser.series


def latest_release_from_deliverable_yaml(content: str) -> OpenStackRelease:
    """Select the final declared release, rejecting empty deliverable inventories."""
    releases = releases_from_deliverable_yaml(content)
    if not releases:
        raise ValueError("No releases found in deliverable YAML")
    return releases[-1]


class DeliverableReleaseParser:
    """Associate each version with its first complete project repository/hash pair."""

    def __init__(self) -> None:
        """Initialize release inventory and pending project evidence."""
        self.releases: list[OpenStackRelease] = []
        self.current: OpenStackRelease | None = None
        self.pending_repo: str | None = None

    def finish_entry(self) -> None:
        """Append a release even when it has no complete project identity."""
        if self.current is not None:
            self.releases.append(self.current)

    def consume(self, line: str) -> None:
        """Start a new version or consider project evidence for the active version."""
        match = _VERSION_RE.match(line)
        if match:
            self.finish_entry()
            self.current = OpenStackRelease(match.group(1).strip(), None, None)
            self.pending_repo = None
            return

        if self.current is not None:
            self._project_line(line, self.current)

    def _project_line(self, line: str, current: OpenStackRelease) -> None:
        """Pair a hash only with its pending repository; never replace the first pair."""
        match = _REPO_RE.match(line)
        if match:
            self.pending_repo = match.group(1).strip()
            return

        match = _HASH_RE.match(line)
        if match:
            if self.pending_repo and current.project_repo is None:
                self.current = OpenStackRelease(
                    current.version, self.pending_repo, match.group(1).strip()
                )

            self.pending_repo = None


def releases_from_deliverable_yaml(content: str) -> list[OpenStackRelease]:
    """Read release versions and their first project identities in declaration order."""
    parser = DeliverableReleaseParser()
    for line in content.splitlines():
        parser.consume(line)

    parser.finish_entry()
    return parser.releases


def parse_openstack_target(openstack_target: str) -> ParsedOpenStackTarget:
    """Validate and normalize a release identifier and optional stage suffix."""
    match = _OPENSTACK_TARGET_RE.fullmatch(openstack_target)
    if not match:
        raise ReleaseDiscoveryError(f"Unsupported OpenStack target: {openstack_target}")
    return ParsedOpenStackTarget(
        release_id=match.group("release_id").lower(), stage=match.group("stage")
    )


def resolve_series(
    series: list[OpenStackSeries], openstack_target: str
) -> OpenStackSeries:
    """Match a requested release by canonical series name or declared release ID."""
    target = parse_openstack_target(openstack_target)
    for entry in series:
        if (
            entry.release_id and entry.release_id.lower() == target.release_id
        ) or entry.name.lower() == target.release_id:
            return entry
    raise ReleaseDiscoveryError(f"Unknown OpenStack series target: {target.release_id}")


def _ends_branch_section(line: str) -> bool:
    """Recognize the next top-level field without treating list entries as fields."""
    return bool(line) and not line.startswith(" ") and not line.startswith("-")


def _branch_lines(content: str) -> Iterator[str | None]:
    """Yield branch fields until the next top-level section starts."""
    in_branches = False
    for line in content.splitlines():
        if line.startswith("branches:"):
            in_branches = True
            # Preserve repeated section headers so consumers reset pending names.
            yield None
            continue

        if not in_branches:
            continue

        if _ends_branch_section(line):
            break

        yield line


def branch_locations_from_deliverable_yaml(content: str) -> dict[str, str]:
    """Read branch locations while preserving the most recent named branch."""
    branches: dict[str, str] = {}
    current_branch: str | None = None
    for line in _branch_lines(content):
        if line is None:
            current_branch = None
            continue

        current_branch = _record_branch_line(line, current_branch, branches)

    return branches


def _record_branch_line(
    line: str, current: str | None, branches: dict[str, str]
) -> str | None:
    """Start a branch or associate a location with the last branch name."""
    match = _BRANCH_NAME_RE.match(line)
    if match:
        return match.group(1).strip()

    match = _BRANCH_LOCATION_RE.match(line)
    if match and current is not None:
        branches[current] = match.group(1).strip()

    return current


def _release_index_by_version(releases: list[OpenStackRelease]) -> dict[str, int]:
    """Index release positions for resolving declared stable branch locations."""
    return {release.version: index for index, release in enumerate(releases)}


def _stable_branch_indexes(
    content: str, releases: list[OpenStackRelease]
) -> list[tuple[int, str]]:
    """Index stable branches only when their location names a known release."""
    locations = branch_locations_from_deliverable_yaml(content)
    indexes = _release_index_by_version(releases)
    return sorted(
        (indexes[location], name)
        for name, location in locations.items()
        if name.startswith("stable/") and location in indexes
    )


def _series_window(
    indexes: list[tuple[int, str]], branch: str, count: int
) -> tuple[int, int]:
    """Bound one series between its neighboring stable branch release indexes."""
    for position, (_, name) in enumerate(indexes):
        if name == branch:
            start = indexes[position - 1][0] + 1 if position > 0 else 0
            end = indexes[position + 1][0] if position + 1 < len(indexes) else count
            return start, end

    return 0, count


def _candidate_releases_for_series(
    content: str, releases: list[OpenStackRelease], release_id: str
) -> list[OpenStackRelease]:
    """Require a known stable branch when branch-index evidence is available."""
    indexes = _stable_branch_indexes(content, releases)
    branch = f"stable/{release_id}"
    if indexes and branch not in {name for _, name in indexes}:
        raise ReleaseDiscoveryError(
            f"Missing stable branch metadata for series {release_id}"
        )

    start, end = _series_window(indexes, branch, len(releases))
    return releases[start:end]


def _numeric_series_prefix(version: str) -> str | None:
    """Extract the leading numeric series component when a version declares one."""
    match = re.match(r"^(?P<prefix>\d+)\.", version)
    if match:
        return match.group("prefix")
    return None


def _independent_release(
    releases: list[OpenStackRelease], stage: str | None
) -> OpenStackRelease:
    """Prefer the last requested independent stage, otherwise the latest release."""
    if stage is not None:
        for release in reversed(releases):
            if release.version.lower().endswith(stage.lower()):
                return release

    return releases[-1]


def _stable_releases(releases: list[OpenStackRelease]) -> list[OpenStackRelease]:
    """Exclude beta and release-candidate suffixes from stable selection."""
    return [
        release for release in releases if not _PRERELEASE_RE.search(release.version)
    ]


def _filter_release_prefix(
    releases: list[OpenStackRelease], prefix: str | None
) -> list[OpenStackRelease]:
    """Constrain versions to a known numeric major series when available."""
    if prefix is None:
        return releases

    return [
        release
        for release in releases
        if _numeric_series_prefix(release.version) == prefix
    ]


def _validate_stage_metadata(
    branch: str, locations: dict[str, str], stable: list[OpenStackRelease], target: str
) -> None:
    """Reject ambiguous staged selection without an explicit stable branch."""
    if branch in locations:
        return

    prefixes = set(
        map(_numeric_series_prefix, (release.version for release in stable))
    ) - {None}
    if len(prefixes) > 1:
        raise ReleaseDiscoveryError(
            f"Cannot safely resolve staged target without stable branch metadata: {target}"
        )


def _staged_candidates(
    content: str,
    candidates: list[OpenStackRelease],
    parsed: ParsedOpenStackTarget,
    target: str,
) -> list[OpenStackRelease]:
    """Narrow staged releases using branch evidence followed by the last stable prefix."""
    locations = branch_locations_from_deliverable_yaml(content)
    branch = f"stable/{parsed.release_id}"
    location = locations.get(branch)
    if location is not None:
        candidates = _filter_release_prefix(
            candidates, _numeric_series_prefix(location)
        )

    stable = _stable_releases(candidates)
    _validate_stage_metadata(branch, locations, stable, target)
    if stable:
        candidates = _filter_release_prefix(
            candidates, _numeric_series_prefix(stable[-1].version)
        )

    return candidates


def _select_series_release(
    content: str,
    candidates: list[OpenStackRelease],
    parsed: ParsedOpenStackTarget,
    target: str,
) -> OpenStackRelease:
    """Choose the latest stable release or the first matching constrained stage."""
    if parsed.stage is None or parsed.stage == "final":
        stable = _stable_releases(candidates)
        if stable:
            return stable[-1]

        raise ReleaseDiscoveryError(f"No stable release found for target: {target}")

    return _matching_stage(
        _staged_candidates(content, candidates, parsed, target), parsed.stage, target
    )


def _matching_stage(
    candidates: list[OpenStackRelease], stage: str, target: str
) -> OpenStackRelease:
    """Select the first matching stage in deliverable order."""
    for release in candidates:
        if release.version.lower().endswith(stage.lower()):
            return release

    raise ReleaseDiscoveryError(f"No matching release found for target: {target}")


def resolve_release_from_deliverable_yaml(
    content: str,
    *,
    openstack_target: str,
    deliverable_scope: str,
) -> OpenStackRelease:
    """Resolve a stable or staged target using the deliverable's declared release order."""
    releases = releases_from_deliverable_yaml(content)
    if not releases:
        raise ReleaseDiscoveryError("No releases found in deliverable YAML")

    parsed = parse_openstack_target(openstack_target)
    if deliverable_scope == "_independent":
        return _independent_release(releases, parsed.stage)

    candidates = _candidate_releases_for_series(content, releases, parsed.release_id)
    return _select_series_release(content, candidates, parsed, openstack_target)


def validate_snapshot_at(snapshot_at: str | None) -> str | None:
    """Validate an optional ISO timestamp while preserving its serialized spelling."""
    if snapshot_at is None:
        return None
    try:
        datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseDiscoveryError(
            f"Invalid snapshot timestamp: {snapshot_at}"
        ) from exc
    return snapshot_at


def derive_deliverable_name(package: PackageDefinition) -> str:
    """Prefer the configured deliverable name, falling back to the upstream repository."""
    if package.openstack_deliverable:
        return package.openstack_deliverable
    upstream_repo = package.upstream_repo.rstrip("/")
    return upstream_repo.rsplit("/", 1)[-1]


class OpenStackReleaseResolver:
    """Resolve release identities while sharing synchronized metadata caches."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENSTACK_RELEASES_BASE_URL,
        fetcher: Callable[[str], str] = read_text_from_url,
    ) -> None:
        """Initialize metadata retrieval configuration and owned state."""
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher
        self._series_status: list[OpenStackSeries] | None = None
        self._deliverable_cache: dict[str, str] = {}
        self._repo_metadata_cache: dict[str, dict] = {}
        self._resolved_cache: dict[
            tuple[str, str, str, str, str | None], ResolvedRelease
        ] = {}
        self._cache_lock = RLock()

    def resolve(
        self,
        *,
        package: PackageDefinition,
        openstack_target: str,
        snapshot_at: str | None = None,
    ) -> ResolvedRelease:
        """Return a cached release identity or resolve it under the shared cache lock."""
        snapshot_at = validate_snapshot_at(snapshot_at)
        deliverable_name = derive_deliverable_name(package)
        cache_key = (
            package.source_package,
            package.upstream_repo,
            deliverable_name,
            openstack_target,
            snapshot_at,
        )
        with self._cache_lock:
            cached = self._resolved_cache.get(cache_key)
            if cached is not None:
                return cached

            resolved_release = self._resolve_uncached(
                package, openstack_target, snapshot_at, deliverable_name
            )
            self._resolved_cache[cache_key] = resolved_release
            return resolved_release

    def _resolve_uncached(
        self,
        package: PackageDefinition,
        openstack_target: str,
        snapshot_at: str | None,
        deliverable_name: str,
    ) -> ResolvedRelease:
        """Resolve a cache miss while the caller holds the metadata cache lock."""
        resolved_series = resolve_series(
            self._series_status_entries(), openstack_target
        )
        deliverable_path, content = self._fetch_deliverable(
            resolved_series.name, deliverable_name
        )
        deliverable_target = _deliverable_target(
            resolved_series, openstack_target, deliverable_path
        )
        release = resolve_release_from_deliverable_yaml(
            content,
            openstack_target=deliverable_target,
            deliverable_scope=deliverable_path.split("/")[-2],
        )
        upstream_ref = release.version
        if snapshot_at:
            if release.project_repo is None:
                raise ReleaseDiscoveryError(
                    f"Snapshot resolution requires a project repo for {deliverable_name}"
                )

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
        return resolved_release

    def _series_status_entries(self) -> list[OpenStackSeries]:
        """Load series status once under the shared cache lock."""
        with self._cache_lock:
            if self._series_status is None:
                self._series_status = parse_series_status_yaml(
                    self.fetcher(f"{self.base_url}/data/series_status.yaml")
                )

            return self._series_status

    def _fetch_deliverable(
        self, series_name: str, deliverable_name: str
    ) -> tuple[str, str]:
        """Try series-specific then independent metadata, caching successful responses."""
        for scope in (series_name, "_independent"):
            path = f"deliverables/{scope}/{deliverable_name}.yaml"
            with self._cache_lock:
                if path in self._deliverable_cache:
                    return path, self._deliverable_cache[path]

                try:
                    content = self.fetcher(f"{self.base_url}/{path}")
                except ReleaseNotFoundError:
                    continue

                self._deliverable_cache[path] = content
                return path, content

        raise ReleaseDiscoveryError(
            f"Deliverable not found for {deliverable_name} in series {series_name}"
        )

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
        """Find the last upstream commit at the requested snapshot timestamp."""
        branch = self._default_branch_for_upstream_repo(upstream_repo)
        branch = _snapshot_branch(
            branch, deliverable_scope, release_id, deliverable_content
        )
        query = urlencode({"sha": branch, "until": snapshot_at, "limit": 1})
        payload = self.fetcher(_canonical_commit_api_url(upstream_repo, query))
        try:
            commits = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ReleaseDiscoveryError(
                f"Invalid commit API response for {project_repo} at {snapshot_at}"
            ) from exc

        if not commits:
            raise ReleaseDiscoveryError(
                f"No upstream commit found for {project_repo} at {snapshot_at}"
            )

        sha = commits[0].get("sha")
        if not sha:
            raise ReleaseDiscoveryError(
                f"Snapshot response did not include a commit SHA for {project_repo}"
            )

        return sha

    def _default_branch_for_upstream_repo(self, upstream_repo: str) -> str:
        """Read and cache repository metadata and require its declared default branch."""
        with self._cache_lock:
            cached = self._repo_metadata_cache.get(upstream_repo)
            if cached is None:
                payload = self.fetcher(_canonical_repo_api_url(upstream_repo))
                try:
                    cached = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ReleaseDiscoveryError(
                        f"Invalid repository API response for {upstream_repo}"
                    ) from exc

                self._repo_metadata_cache[upstream_repo] = cached

        default_branch = cached.get("default_branch")
        if not default_branch:
            raise ReleaseDiscoveryError(
                f"Repository API response did not include a default branch for {upstream_repo}"
            )

        return default_branch


def _deliverable_target(series: OpenStackSeries, target: str, path: str) -> str:
    """Use the resolved series ID only for independently released deliverables."""
    if path.split("/")[-2] == "_independent":
        return series.release_id or target

    return target


def _snapshot_branch(
    default: str, scope: str, release_id: str | None, content: str
) -> str:
    """Prefer an explicitly declared stable branch for series-scoped snapshots."""
    if scope != "_independent" and release_id is not None:
        locations = branch_locations_from_deliverable_yaml(content)
        if f"stable/{release_id}" in locations:
            return f"stable/{release_id}"

    return default


def _canonical_commit_api_url(upstream_repo: str, query: str) -> str:
    """Construct the repository commit API URL with an already encoded query."""
    parsed = urlparse(upstream_repo)
    if not parsed.scheme or not parsed.netloc or not parsed.path:
        raise ReleaseDiscoveryError(
            f"Unsupported upstream repository URL: {upstream_repo}"
        )
    repo_path = parsed.path.strip("/")
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/repos/{repo_path}/commits?{query}"


def _canonical_repo_api_url(upstream_repo: str) -> str:
    """Construct the repository metadata API URL after checking its basic URL shape."""
    parsed = urlparse(upstream_repo)
    if not parsed.scheme or not parsed.netloc or not parsed.path:
        raise ReleaseDiscoveryError(
            f"Unsupported upstream repository URL: {upstream_repo}"
        )
    repo_path = parsed.path.strip("/")
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/repos/{repo_path}"
