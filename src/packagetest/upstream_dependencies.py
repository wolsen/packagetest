"""Read Python dependency metadata from an immutable upstream revision."""

from __future__ import annotations

import re
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from typing import NotRequired, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from .contracts import object_list, object_mapping, string_list, text_field

DEPENDENCY_FILES = ("requirements.txt", "test-requirements.txt", "pyproject.toml")
TEST_GROUP_NAMES = {"test", "tests", "testing"}
ARCHIVE_DISTRIBUTION_ALIASES = {
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
}


class RequirementRecord(TypedDict):
    """A Python requirement declaration and optional provider-resolution evidence."""

    distribution: str | None
    requirement: str
    kind: str
    file: str
    line: int
    group: NotRequired[str]
    source: NotRequired[str]
    archive_version: NotRequired[str | None]
    archive_binary: NotRequired[str]
    archive_source: NotRequired[str]
    archive_satisfies: NotRequired[bool | None]
    archive_decision: NotRequired[str]
    archive_decision_reason: NotRequired[str]


class ArchiveProvider(TypedDict):
    """An unambiguous Python distribution supplied by a published Debian binary."""

    distribution: str
    binary: str
    source: str
    version: str


class MetadataFetchError(RuntimeError):
    """An upstream dependency file could not be read reliably."""


def normalize_distribution(name: str) -> str:
    """Use the same spelling equivalence as Python package indexes."""
    return re.sub(r"[-_.]+", "-", name).lower()


def debian_upstream_version(version: str) -> str:
    """Extract and normalize the upstream portion of a Debian version."""
    value = version.split(":", 1)[-1]
    if "-" in value:
        value = value.rsplit("-", 1)[0]

    # Debian sorts '~' before the empty string; Python expresses the common
    # OpenStack prereleases without that separator (1.0.0rc1).
    return value.replace("~", "")


def archive_requirement_decision(
    requirement: str, archive_version: str
) -> tuple[bool | None, str]:
    """Prove whether an Ubuntu source version satisfies one Python requirement.

    ``None`` is intentionally conservative: an unrepresentable Debian or PEP
    440 version causes a same-run candidate build instead of an archive guess.
    Environment markers are preserved as evidence but are not evaluated using
    the planner host, whose Python version can differ from Ubuntu 26.04.
    """
    value = re.split(r"\s+#", requirement.strip(), maxsplit=1)[0].strip()
    try:
        parsed = Requirement(value)
    except InvalidRequirement as exc:
        return None, f"requirement is not valid PEP 508: {exc}"

    if parsed.url:
        return (
            None,
            "direct URL requirements cannot be satisfied from an Ubuntu version",
        )

    if not parsed.specifier:
        return (
            True,
            "Ubuntu provides the mapped distribution; no version constraint was declared",
        )

    return _version_decision(parsed, archive_version)


def _version_decision(
    parsed: Requirement, archive_version: str
) -> tuple[bool | None, str]:
    """Compare a parsed requirement with the archive's upstream version."""
    upstream = debian_upstream_version(archive_version)
    try:
        satisfied = parsed.specifier.contains(Version(upstream), prereleases=True)
    except InvalidVersion as exc:
        return None, f"Ubuntu upstream version {upstream!r} is not valid PEP 440: {exc}"

    relation = "satisfies" if satisfied else "does not satisfy"
    return (
        satisfied,
        f"Ubuntu {archive_version} ({upstream}) {relation} {parsed.specifier}",
    )


def requirement_name(requirement: str) -> str | None:
    """Return the distribution name from a PEP 508-style requirement.

    OpenStack requirements use ordinary distribution names followed by a
    version, extra, marker, or URL. Options and editable VCS requirements do
    not name a distribution unambiguously and are retained as unresolved
    evidence instead of guessed.
    """
    value = requirement.strip()
    if not value or value.startswith(("#", "-")):
        return None

    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[|[<>=!~;@\s]|$)", value)
    return normalize_distribution(match.group(1)) if match else None


def parse_requirement_file(
    text: str, *, filename: str, kind: str
) -> list[RequirementRecord]:
    """Preserve line-numbered requirement evidence, including unresolved options."""
    records: list[RequirementRecord] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue

        name = requirement_name(value)
        records.append(
            {
                "distribution": name,
                "requirement": value,
                "kind": kind,
                "file": filename,
                "line": line_number,
            }
        )

    return records


def _string_requirements(
    values: object, *, filename: str, kind: str, group: str | None = None
) -> list[RequirementRecord]:
    """Read literal requirements, leaving structured dependency-group includes unresolved."""
    records: list[RequirementRecord] = []
    for index, value in enumerate(
        object_list(values, f"{filename} {kind} requirements"), 1
    ):
        if not isinstance(value, str):
            continue

        record: RequirementRecord = {
            "distribution": requirement_name(value),
            "requirement": value,
            "kind": kind,
            "file": filename,
            "line": index,
        }
        if group:
            record["group"] = group

        records.append(record)

    return records


def parse_pyproject(text: str) -> list[RequirementRecord]:
    """Read runtime, build-system, and named test requirements from TOML tables."""
    data = tomllib.loads(text)
    records: list[RequirementRecord] = []
    project = object_mapping(data.get("project", {}), "project")
    build = object_mapping(data.get("build-system", {}), "build-system")
    records.extend(
        _string_requirements(
            project.get("dependencies", []), filename="pyproject.toml", kind="runtime"
        )
    )
    records.extend(
        _string_requirements(
            build.get("requires", []), filename="pyproject.toml", kind="build-system"
        )
    )
    optional = object_mapping(
        project.get("optional-dependencies", {}), "optional-dependencies"
    )
    for group, values in optional.items():
        if normalize_distribution(group) in TEST_GROUP_NAMES:
            records.extend(
                _string_requirements(
                    values, filename="pyproject.toml", kind="test", group=group
                )
            )

    for group, values in object_mapping(
        data.get("dependency-groups", {}), "dependency-groups"
    ).items():
        if normalize_distribution(group) in TEST_GROUP_NAMES:
            records.extend(
                _string_requirements(
                    values, filename="pyproject.toml", kind="test", group=group
                )
            )

    return records


def raw_url(repository: str, sha: str, filename: str) -> str:
    """Construct a supported host's raw URL pinned to the selected Git revision."""
    parsed = urlparse(repository.rstrip("/"))
    path = parsed.path.strip("/").removesuffix(".git")
    if parsed.hostname == "opendev.org":
        return f"{parsed.scheme}://{parsed.netloc}/{path}/raw/commit/{sha}/{filename}"

    if parsed.hostname in {"github.com", "www.github.com"}:
        return f"https://raw.githubusercontent.com/{path}/{sha}/{filename}"

    raise MetadataFetchError(
        f"Unsupported upstream source host for dependency metadata: {repository}"
    )


def fetch_optional(url: str, *, attempts: int = 3, timeout: int = 30) -> str | None:
    """Retry transient fetch failures while treating absent files as optional."""
    if min(attempts, timeout) < 1:
        raise ValueError("Metadata fetch attempts and timeout must be positive")

    request = Request(url, headers={"User-Agent": "packagetest-dependency-planner/1"})
    for attempt in range(attempts):
        try:
            return _fetch_dependency_file(request, timeout)
        except (URLError, TimeoutError, UnicodeDecodeError) as exc:
            error = exc

        if attempt + 1 < attempts:
            time.sleep(1 << attempt)

    raise MetadataFetchError(f"Failed to fetch {url}: {error}")


def _fetch_dependency_file(request: Request, timeout: int) -> str | None:
    """Read one metadata file, distinguishing absence from retrieval failure."""
    try:
        with urlopen(request, timeout=timeout) as response:
            payload: bytes = response.read()
            return payload.decode()
    except HTTPError as exc:
        if exc.code == 404:
            return None

        raise


def inspect_revision(
    entry: Mapping[str, object], *, fetch: Callable[[str], str | None] = fetch_optional
) -> tuple[list[RequirementRecord], list[str]]:
    """Read available dependency metadata from the immutable upstream SHA."""
    if not entry.get("upstream_sha"):
        raise MetadataFetchError(f"{entry['source']} has no immutable upstream SHA")

    records = []
    present = []
    for filename in DEPENDENCY_FILES:
        url = raw_url(
            text_field(entry, "upstream_repository"),
            text_field(entry, "upstream_sha"),
            filename,
        )
        text = fetch(url)
        if text is None:
            continue

        present.append(filename)
        records.extend(_parse_dependency_file(filename, text))

    return records, present


def _parse_dependency_file(filename: str, text: str) -> list[RequirementRecord]:
    """Choose the metadata parser and requirement kind for a known file."""
    if filename == "requirements.txt":
        return parse_requirement_file(text, filename=filename, kind="runtime")

    if filename == "test-requirements.txt":
        return parse_requirement_file(text, filename=filename, kind="test")

    return parse_pyproject(text)


def _distribution_names(entry: Mapping[str, object]) -> set[str]:
    """Collect source, deliverable, and binary spellings for one producer."""
    source = text_field(entry, "source")
    names = {source, text_field(entry, "deliverable")}
    for prefix in ("python-", "python3-"):
        if source.startswith(prefix):
            names.add(source[len(prefix) :])

    for binary in string_list(entry.get("binaries", []), "binaries"):
        names.update((binary, binary.removeprefix("python3-")))

    return names - {""}


def _add_source_aliases(
    candidates: dict[str, set[str]], packages: Sequence[Mapping[str, object]]
) -> None:
    """Resolve known import/distribution differences through binary providers."""
    aliases = {"pyyaml": "python3-yaml", "python-dateutil": "python3-dateutil"}
    binary_sources = {
        binary: text_field(entry, "source")
        for entry in packages
        for binary in string_list(entry.get("binaries", []), "binaries")
    }
    for distribution, binary in aliases.items():
        if binary in binary_sources:
            candidates.setdefault(distribution, set()).add(binary_sources[binary])


def distribution_source_index(
    packages: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    """Map unambiguous Python distribution spellings to Ubuntu sources."""
    candidates: dict[str, set[str]] = {}
    for entry in packages:
        source = text_field(entry, "source")
        for name in _distribution_names(entry):
            candidates.setdefault(normalize_distribution(name), set()).add(source)

    _add_source_aliases(candidates, packages)
    return {
        name: next(iter(sources))
        for name, sources in candidates.items()
        if len(sources) == 1
    }


def distribution_archive_index(
    records: Sequence[Mapping[str, object]],
) -> dict[str, ArchiveProvider]:
    """Return unambiguous Python distribution providers from Ubuntu indexes."""
    candidates: dict[str, list[ArchiveProvider]] = {}
    for original in records:
        record = ArchiveProvider(
            distribution=text_field(original, "distribution"),
            binary=text_field(original, "binary"),
            source=text_field(original, "source"),
            version=text_field(original, "version"),
        )
        distribution = normalize_distribution(record["distribution"])
        candidates.setdefault(distribution, []).append(record)

    _add_archive_aliases(candidates)
    return {
        name: values[0]
        for name, values in candidates.items()
        if _single_archive_provider(values)
    }


def _add_archive_aliases(candidates: dict[str, list[ArchiveProvider]]) -> None:
    """Make known distribution aliases refer to the same archive candidates."""
    for alias, distribution in ARCHIVE_DISTRIBUTION_ALIASES.items():
        if distribution in candidates:
            candidates.setdefault(alias, []).extend(candidates[distribution])


def _single_archive_provider(values: list[ArchiveProvider]) -> bool:
    """Accept repeated observations only when the binary and source agree."""
    return len({(value["binary"], value["source"]) for value in values}) == 1


class RequirementMapper:
    """Apply one consumer's provider policy while collecting candidate sources."""

    def __init__(
        self,
        index: dict[str, str],
        consumer: str,
        packages: Mapping[str, Mapping[str, object]],
        archive_index: Mapping[str, ArchiveProvider],
    ) -> None:
        """Capture the frozen source and binary indexes for this consumer."""
        self.index = index
        self.consumer = consumer
        self.packages = packages
        self.archive_index = archive_index
        self.dependencies: set[str] = set()

    def map(self, original: RequirementRecord) -> RequirementRecord:
        """Copy evidence and select its source, archive, or unresolved provider."""
        record = original.copy()
        distribution = record["distribution"]
        source = self.index.get(distribution) if distribution else None
        if source and source != self.consumer:
            self._candidate(record, source)
        elif source == self.consumer:
            record["source"] = source
            record["archive_decision_reason"] = (
                "Requirement is provided by the source being built"
            )
        else:
            self._archive(record)

        return record

    def _candidate(self, record: RequirementRecord, source: str) -> None:
        """Require a candidate unless archive version evidence proves sufficiency."""
        record["source"] = source
        package = self.packages.get(source, {})
        raw_version = package.get("archive_version")
        version = (
            None if raw_version is None else text_field(package, "archive_version")
        )
        record["archive_version"] = version
        if version:
            satisfied, reason = archive_requirement_decision(
                record["requirement"], version
            )
        else:
            satisfied, reason = (
                None,
                "mapped Ubuntu source has no archive version evidence",
            )

        _record_archive_decision(record, satisfied, reason)
        if not satisfied:
            self.dependencies.add(source)

    def _archive(self, record: RequirementRecord) -> None:
        """Record binary evidence or explain why no unambiguous provider exists."""
        distribution = record["distribution"]
        if distribution is not None and distribution in self.archive_index:
            provider = self.archive_index[distribution]
            record["archive_binary"] = provider["binary"]
            record["archive_source"] = provider["source"]
            record["archive_version"] = provider["version"]
            satisfied, reason = archive_requirement_decision(
                record["requirement"], provider["version"]
            )
            _record_archive_decision(record, satisfied, reason)
        elif record.get("distribution"):
            record["archive_decision"] = "unmapped"
            record["archive_decision_reason"] = (
                "No unambiguous Ubuntu python3 binary provider was found"
            )


def _record_archive_decision(
    record: RequirementRecord, satisfied: bool | None, reason: str
) -> None:
    """Preserve the three-state distinction between disproven and unknown."""
    record["archive_satisfies"] = satisfied
    record["archive_decision"] = (
        "satisfied"
        if satisfied is True
        else "insufficient"
        if satisfied is False
        else "unknown"
    )
    record["archive_decision_reason"] = reason


def map_requirements(
    records: Sequence[RequirementRecord],
    index: dict[str, str],
    consumer: str,
    packages: Mapping[str, Mapping[str, object]] | None = None,
    archive_index: Mapping[str, ArchiveProvider] | None = None,
) -> tuple[list[RequirementRecord], list[str]]:
    """Map requirement evidence and return the sorted candidate dependency set."""
    mapper = RequirementMapper(index, consumer, packages or {}, archive_index or {})
    mapped = [mapper.map(record) for record in records]
    return mapped, sorted(mapper.dependencies)
