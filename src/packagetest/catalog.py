"""Auditable OpenStack release scope joined to Ubuntu source indexes.

The archive indexes establish package names, not release membership. Membership
comes from the pinned OpenStack releases checkout. Nothing silently disappears
when Ubuntu has no source package for a deliverable.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import re
import shlex
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import partial
from pathlib import Path
from typing import Any
from .repositories import binary_indexes

from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ALIASES = {
    "keystoneauth": "python-keystoneauth1",
    "puppet-openstack_extras": "puppet-module-openstack-extras",
}

# Explicit upstream migrations/retirements; archive dependencies still need a
# source snapshot, but a tombstone branch is not usable source code.
UPSTREAM_OVERRIDES = {
    "python-gnocchiclient": {
        "upstream_repository": "https://github.com/gnocchixyz/python-gnocchiclient",
        "upstream_ref": "master",
        "branch_policy": "maintained-upstream-moved-from-opendev",
        "snapshot_version_backend": "setuptools-scm",
    },
    "python-requestsexceptions": {
        "upstream_ref": "bb64d8a07b515947cf000c375b017026a01f7a4f",
        "branch_policy": "last-source-commit-before-upstream-retirement",
    },
}

# Ubuntu source metadata can retain an obsolete Debian Vcs-Git path after the
# OpenStack packaging team reorganizes Salsa subgroups.
PACKAGING_REPOSITORY_OVERRIDES = {
    "python-cyborgclient": "https://salsa.debian.org/openstack-team/clients/python-cyborgclient.git",
}

ARCHIVE_COMPONENTS = ("main", "universe")


def ubuntu_source_indexes(suite: str) -> list[dict[str, str]]:
    """Return the live Ubuntu source indexes that define a nightly baseline."""
    indexes = []
    for pocket, host in (
        ("", "https://archive.ubuntu.com/ubuntu"),
        ("-updates", "https://archive.ubuntu.com/ubuntu"),
        ("-security", "https://security.ubuntu.com/ubuntu"),
        ("-proposed", "https://archive.ubuntu.com/ubuntu"),
    ):
        distribution = suite + pocket
        for component in ARCHIVE_COMPONENTS:
            indexes.append(
                {
                    "filename": f"packagetest-{distribution}-{component}-Sources.xz",
                    "url": f"{host}/dists/{distribution}/{component}/source/Sources.xz",
                }
            )

    return indexes


def download_source_indexes(
    suite: str,
    destination: Path,
    *,
    opener: Callable[..., Any] | None = None,
    extra_indexes: list[dict] | None = None,
) -> list[Path]:
    """Download current indexes atomically instead of relying on committed output."""
    opener = opener or urlopen
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for spec in [*ubuntu_source_indexes(suite), *(extra_indexes or [])]:
        path = destination / spec["filename"]
        temporary = path.with_suffix(path.suffix + ".part")
        request = Request(spec["url"], headers={"User-Agent": "packagetest-catalog/1"})
        with opener(request, timeout=90) as response, temporary.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)

        temporary.replace(path)
        paths.append(path)

    return paths


def _archive_requests(catalog: dict) -> list[tuple[str, str, int | None]]:
    """Enumerate all selected source files, including descriptor-only records."""
    files = []
    for package in catalog["packages"]:
        source = package["archive_source"]
        records = source.get("files") or [{"url": source["url"], "size": None}]
        files.extend(
            (package["source"], record["url"], record.get("size")) for record in records
        )

    return files


def _check_archive_size(expected_size: int | None, reported_size: str | None) -> None:
    """Reject a size mismatch when both the index and server supply a size."""
    if expected_size is None or reported_size is None:
        return

    if int(reported_size) != expected_size:
        raise ValueError(
            f"expected {expected_size} bytes, archive reports {reported_size}"
        )


def _check_archive_file(
    item: tuple[str, str, int | None], *, opener: Callable[..., Any]
) -> None:
    """Probe availability with HEAD, falling back only when HEAD is unsupported."""
    _, url, expected_size = item
    request = Request(
        url, headers={"User-Agent": "packagetest-catalog/1"}, method="HEAD"
    )
    try:
        with opener(request, timeout=30) as response:
            _check_archive_size(expected_size, response.headers.get("Content-Length"))
    except HTTPError as exc:
        if exc.code not in {405, 501}:
            raise

        request = Request(
            url, headers={"User-Agent": "packagetest-catalog/1", "Range": "bytes=0-0"}
        )
        with opener(request, timeout=30):
            pass


def _archive_failures(
    files: list[tuple[str, str, int | None]],
    *,
    opener: Callable[..., Any],
    max_workers: int,
) -> list[str]:
    """Collect concurrent archive failures in original file order."""
    failures = []
    with ThreadPoolExecutor(max_workers=max_workers) as workers:
        check = partial(_check_archive_file, opener=opener)
        futures = {workers.submit(check, item): item for item in files}
        for future, item in futures.items():
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{item[0]}: {item[1]}: {exc}")

    return failures


def _preflight_error(failures: list[str]) -> str:
    """Bound archive diagnostics while retaining the total failure count."""
    details = "\n".join(failures[:20])
    suffix = f"\n... and {len(failures) - 20} more" if len(failures) > 20 else ""
    return f"{len(failures)} Ubuntu source files failed preflight:\n{details}{suffix}"


def validate_archive_sources(
    catalog: dict, *, opener: Callable[..., Any] | None = None, max_workers: int = 24
) -> None:
    """Fail planning when any source file selected from the fresh index is absent."""
    failures = _archive_failures(
        _archive_requests(catalog), opener=opener or urlopen, max_workers=max_workers
    )
    if failures:
        raise ValueError(_preflight_error(failures))


def _paragraph_fields(paragraph: str) -> dict[str, str]:
    """Parse Debian continuation lines into the preceding field's value."""
    item = {}
    key = None
    for line in paragraph.splitlines():
        if line[:1].isspace() and key:
            item[key] += "\n" + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            item[key] = value.strip()

    return item


def paragraphs(text: str) -> list[dict[str, str]]:
    """Read nonempty Debian control paragraphs in input order."""
    return [
        item
        for paragraph in text.split("\n\n")
        if (item := _paragraph_fields(paragraph))
    ]


def dependency_names(expression: str) -> set[str]:
    """Conservative names across alternatives, architectures and build profiles.

    This is discovery, not an APT solver. Retaining every alternative prevents
    an OpenStack producer from being omitted; the actual solver checks versions.
    """
    expression = re.sub(r"\([^)]*\)|\[[^]]*\]|<[^>]*>", "", expression)
    return {
        match.group(1)
        for group in re.split("[,|]", expression)
        if (match := re.match(r"\s*([a-z0-9][a-z0-9+.-]*)", group))
    }


def _openstack_namespace(host: str | None, path: str) -> bool:
    """Recognize reviewed OpenStack owners on archive and upstream hosting sites."""
    if host in {"docs.openstack.org", "www.openstack.org", "openstack.org"}:
        return True

    prefixes = {
        "salsa.debian.org": "/openstack-team/",
        "anonscm.debian.org": "/git/openstack/",
        "git.launchpad.net": "/~ubuntu-openstack-dev/",
        "opendev.org": "/openstack/",
        "github.com": "/openstack/",
        "git.openstack.org": "/openstack/",
    }
    prefix = prefixes.get(host)
    return prefix is not None and path.startswith(prefix)


def identity_evidence(archive: dict) -> dict[str, str]:
    """Require OpenStack provenance; package-name equality alone is unsafe.

    Ubuntu also ships unrelated genomics Bifrost, Java Trove, and C++ Taskflow.
    VCS ownership and upstream project namespaces distinguish those packages.
    """
    evidence = {}
    for field in ("Homepage", "Vcs-Git", "Vcs-Browser"):
        value = archive.get(field, "")
        parsed = urlparse(value.split(" ", 1)[0])
        host, path = parsed.hostname, parsed.path
        if _openstack_namespace(host, path):
            evidence[field] = value

    return evidence


def source_name(deliverable: str, sources: dict) -> str | None:
    """Resolve a deliverable alias only when archive metadata proves OpenStack ownership."""
    for candidate in (
        ALIASES.get(deliverable),
        deliverable,
        "python-" + deliverable,
        "openstack-" + deliverable,
        deliverable.replace("puppet-", "puppet-module-", 1),
    ):
        if candidate in sources and identity_evidence(sources[candidate]):
            return candidate

    return None


def vcs_git(value: str) -> tuple[str | None, str | None]:
    """Split a Debian Vcs-Git URL and its optional ``-b`` branch hint."""
    words = shlex.split(value or "")
    if not words:
        return None, None

    branch = (
        words[words.index("-b") + 1]
        if "-b" in words and words.index("-b") + 1 < len(words)
        else None
    )
    return words[0], branch


def repository_names(metadata: dict) -> list[str]:
    """Collect unique upstream repositories from settings and release history."""
    names = set(metadata.get("repository-settings", {}))
    for release in metadata.get("releases", []):
        names.update(p["repo"] for p in release.get("projects", []))

    return sorted(names)


def _project_revision(project: dict, repository: str | None) -> str | None:
    """Accept only a full immutable hash for the chosen upstream repository."""
    revision = project.get("hash")
    if project.get("repo") != repository or repository is None:
        return None

    if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
        return revision

    return None


def _series_release_selection(
    metadata: dict, repository: str | None
) -> tuple[str, str, str | None]:
    """Choose the latest matching immutable release from a cycle-scoped history."""
    for release in reversed(metadata.get("releases", [])):
        for project in release.get("projects", []):
            revision = _project_revision(project, repository)
            if revision is not None:
                return (
                    revision,
                    "release-metadata-series-release",
                    release.get("version"),
                )

    return "master", "release-metadata-no-series-ref", None


def _upstream_selection(
    metadata: dict, repository: str | None, *, series: str, membership: str, status: str
) -> tuple[str, str, str | None]:
    """Select the live series branch or the best series-scoped immutable ref."""
    stable = f"stable/{series}"
    branches = {branch["name"] for branch in metadata.get("branches", [])}
    if stable in branches:
        return stable, "release-metadata-stable", None

    if status == "development":
        return "master", "release-metadata-development-branch", None

    # Cycle deliverable files contain only releases assigned to this series.
    # Independent deliverables span many series and need upper-constraints
    # selection before one of their release hashes can safely be chosen.
    if membership == "cycle":
        return _series_release_selection(metadata, repository)

    return "master", "release-metadata-no-series-ref", None


def _select_repository(name: str, repositories: list[str]) -> str | None:
    """Choose an exact deliverable match or an unambiguous sole repository."""
    exact = [repo for repo in repositories if repo.rsplit("/", 1)[-1] == name]
    if len(exact) == 1:
        return exact[0]

    if len(repositories) == 1:
        return repositories[0]

    return None


def _archive_source(archive: dict) -> dict:
    """Build a downloadable source descriptor from strong archive checksums."""
    algorithm = "sha256" if archive.get("Checksums-Sha256") else "sha512"
    checksum_field = "Checksums-" + algorithm.capitalize()
    if not archive.get(checksum_field):
        raise ValueError(f"{archive['Package']} source index has no strong checksums")

    archive_files = _archive_files(archive, checksum_field, algorithm)
    dsc = _source_descriptor(archive["Package"], archive_files)
    return {"url": dsc["url"], algorithm: dsc[algorithm], "files": archive_files}


def _archive_files(archive: dict, checksum_field: str, algorithm: str) -> list[dict]:
    """Preserve index order while resolving source file URLs and sizes."""
    checksums = [
        line.split() for line in archive[checksum_field].splitlines() if line.strip()
    ]
    base_url = archive.get(
        "_packagetest_archive_root", "https://archive.ubuntu.com/ubuntu/"
    )
    base_url = base_url.rstrip("/") + "/" + archive["Directory"] + "/"
    return [
        {
            "name": row[2],
            "url": base_url + row[2],
            algorithm: row[0],
            "size": int(row[1]),
        }
        for row in checksums
    ]


def _source_descriptor(source: str, files: list[dict]) -> dict:
    """Require a source descriptor among the indexed archive files."""
    for item in files:
        if item["name"].endswith(".dsc"):
            return item

    raise ValueError(f"No .dsc checksum for {source}")


def _comparison_fields(comparison: dict | None) -> dict:
    """Represent an absent target baseline explicitly instead of using Ubuntu's version."""
    comparison = comparison or {}
    return {
        "comparison_archive_version": comparison.get("Version"),
        "comparison_archive_pocket": comparison.get("_packagetest_archive_pocket"),
        "comparison_archive_index": comparison.get("_packagetest_archive_index"),
    }


def _binary_names(archive: dict) -> list[str]:
    """Normalize comma-separated binary names without retaining empty entries."""
    return sorted(
        name.strip() for name in archive.get("Binary", "").split(",") if name.strip()
    )


def _build_depends(archive: dict) -> str:
    """Join build dependency fields in their established discovery order."""
    return ", ".join(
        archive[key]
        for key in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch")
        if archive.get(key)
    )


def _importer_branches(suite: str, archive: dict) -> list[str]:
    """Prefer importer branches for the selected pocket before release branches."""
    pocket = archive.get("_packagetest_archive_pocket")
    branches = []
    if pocket == "security":
        branches.append(f"ubuntu/{suite}-security")

    if pocket in {"security", "updates"}:
        branches.append(f"ubuntu/{suite}-updates")

    return [*branches, f"ubuntu/{suite}", f"ubuntu/{suite}-devel"]


def _packaging_fields(
    archive: dict, series: str, codename: str | None, suite: str
) -> dict:
    """Describe packaging repositories and ordered branch candidates for this target."""
    codename = codename or series_name(series)
    archive_packaging_repository, archive_packaging_branch = vcs_git(
        archive.get("Vcs-Git", "")
    )
    archive_packaging_repository = PACKAGING_REPOSITORY_OVERRIDES.get(
        archive["Package"], archive_packaging_repository
    )
    return {
        "archive_packaging_repository": archive_packaging_repository,
        "archive_packaging_branch": archive_packaging_branch,
        "archive_packaging_series_branch": (f"debian/{codename}"),
        "packaging_repository": (
            "https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/"
            f"{archive['Package']}/+git/{archive['Package']}"
        ),
        "packaging_branch_candidates": [f"stable/{series}", "master"],
        "ubuntu_importer_repository": f"https://git.launchpad.net/ubuntu/+source/{archive['Package']}",
        "ubuntu_importer_branch_candidates": _importer_branches(suite, archive),
        "packaging_upstream_branch_candidates": [f"upstream-{codename}", "upstream"],
        "packaging_pristine_tar_branch": "pristine-tar",
    }


def package_record(
    name: str,
    metadata: dict,
    archive: dict,
    *,
    series: str,
    membership: str,
    series_status: str = "unknown",
    codename: str | None = None,
    suite: str = "resolute",
    comparison_archive: dict | None = None,
    comparison_required: bool = False,
) -> dict:
    """Join upstream selection and archive evidence into one auditable package record."""
    repos = repository_names(metadata)
    repo = _select_repository(name, repos)
    upstream_ref, branch_policy, upstream_release = _upstream_selection(
        metadata, repo, series=series, membership=membership, status=series_status
    )
    archive_source = _archive_source(archive)
    comparison = comparison_archive if comparison_required else archive
    return {
        "deliverable": name,
        "source": archive["Package"],
        "membership": membership,
        "release_type": metadata.get("type", "other"),
        "team": metadata.get("team"),
        "upstream_repository": "https://opendev.org/" + repo if repo else None,
        "upstream_ref": upstream_ref,
        "branch_policy": branch_policy,
        "upstream_release": upstream_release,
        "series_status": series_status,
        "all_upstream_repositories": repos,
        "archive_version": archive["Version"],
        "archive_pocket": archive.get("_packagetest_archive_pocket", "release"),
        "archive_index": archive.get("_packagetest_archive_index"),
        **_comparison_fields(comparison),
        "archive_identity_evidence": identity_evidence(archive),
        "binaries": _binary_names(archive),
        "build_depends": _build_depends(archive),
        "testsuite": archive.get("Testsuite", ""),
        **_packaging_fields(archive, series, codename, suite),
        "archive_source": archive_source,
        "snapshot_backend": "git-archive"
        if name.startswith("puppet-")
        else "python-sdist",
        "discovery_error": None
        if repo
        else "Multiple or missing upstream repositories require explicit mapping",
    }


def series_name(series: str) -> str:
    """Map the active numeric OpenStack series to its release codename."""
    # Keep this explicit: guessing a future codename would select the wrong
    # packaging upstream branch while still producing a syntactically valid ref.
    names = {"2026.2": "hibiscus", "2027.1": "indri"}
    if series not in names:
        raise ValueError(f"No packaging upstream branch mapping for OpenStack {series}")

    return names[series]


def archive_python_packages(sources: dict[str, dict]) -> list[dict]:
    """Retain Ubuntu's Python binary providers for upstream dependency checks.

    The OpenStack catalog deliberately contains only release deliverables and
    their OpenStack build closure.  Test requirements such as ``gabbi`` and
    ``wsgi-intercept`` are supplied by unrelated Ubuntu source packages, so a
    second lightweight index is needed to prove their availability without
    adding them to the OpenStack build DAG.
    """
    records = []
    for source, item in sorted(sources.items()):
        for binary in _binary_names(item):
            if not re.fullmatch(r"python3-[a-z0-9][a-z0-9+.-]*", binary):
                continue

            records.append(
                {
                    "distribution": re.sub(
                        r"[-_.]+", "-", binary.removeprefix("python3-")
                    ).lower(),
                    "binary": binary,
                    "source": source,
                    "version": item["Version"],
                }
            )

    return records


def _index_text(path: Path, data: bytes) -> str:
    """Decode a source index using the compression declared by its suffix."""
    decompress = {".xz": lzma.decompress, ".gz": gzip.decompress}.get(path.suffix)
    if decompress is not None:
        data = decompress(data)

    return data.decode()


def _archive_pocket(filename: str, suite: str, metadata: dict) -> str:
    """Prefer explicit archive metadata over Ubuntu filename conventions."""
    if metadata.get("pocket"):
        return metadata["pocket"]

    for pocket in ("security", "updates", "proposed"):
        if f"{suite}-{pocket}-" in filename:
            return pocket

    return "release"


def _preferred_source(candidate: dict, previous: dict | None) -> bool:
    """Rank archive tiers before Debian versions within the same tier."""
    if previous is None:
        return True

    priority = candidate["_packagetest_archive_priority"]
    previous_priority = previous.get("_packagetest_archive_priority", -1)
    if priority != previous_priority:
        return priority > previous_priority

    return (
        subprocess.run(
            [
                "dpkg",
                "--compare-versions",
                candidate["Version"],
                "gt",
                previous["Version"],
            ]
        ).returncode
        == 0
    )


@dataclass
class ArchiveSelection:
    """Own selected source versions and comparison baselines across index files."""

    sources: dict[str, dict] = dataclass_field(default_factory=dict)
    comparison_sources: dict[str, dict] = dataclass_field(default_factory=dict)
    indexes: list[dict[str, str]] = dataclass_field(default_factory=list)

    def add_index(self, path: Path, suite: str, metadata: dict) -> None:
        """Record input provenance and merge its annotated source paragraphs."""
        data = path.read_bytes()
        self.indexes.append(
            {"filename": path.name, "sha256": hashlib.sha256(data).hexdigest()}
        )
        for source in paragraphs(_index_text(path, data)):
            if "Package" not in source:
                continue

            source.update(
                {
                    "_packagetest_archive_pocket": _archive_pocket(
                        path.name, suite, metadata
                    ),
                    "_packagetest_archive_root": metadata.get(
                        "archive_root", "https://archive.ubuntu.com/ubuntu/"
                    ),
                    "_packagetest_archive_index": path.name,
                    "_packagetest_archive_priority": int(
                        metadata.get("comparison_priority", 0)
                    ),
                }
            )
            self.add_source(source)

    def add_source(self, source: dict) -> None:
        """Retain the preferred package and any target-specific comparison record."""
        name = source["Package"]
        # A UCA overlay defines comparison even when Ubuntu has a newer version.
        if source["_packagetest_archive_priority"]:
            self._retain(self.comparison_sources, name, source)

        self._retain(self.sources, name, source)

    @staticmethod
    def _retain(records: dict[str, dict], name: str, candidate: dict) -> None:
        """Replace one archive record only when its tier or version wins."""
        if _preferred_source(candidate, records.get(name)):
            records[name] = candidate


def _series_status(releases: Path, codename: str, series: str) -> str:
    """Read the matching series status, tolerating absent release-status metadata."""
    import yaml

    path = releases / "data/series_status.yaml"
    if not path.is_file():
        return "unknown"

    for item in yaml.safe_load(path.read_text()):
        if item.get("name") == codename or str(item.get("release-id")) == str(series):
            return item.get("status", "unknown")

    return "unknown"


def _deliverables(releases: Path, codename: str) -> dict[str, dict]:
    """Load deliverables in filename order for deterministic catalog assembly."""
    import yaml

    return {
        path.stem: yaml.safe_load(path.read_text())
        for path in sorted((releases / "deliverables" / codename).glob("*.yaml"))
    }


def _binary_providers(packages: dict[str, dict]) -> dict[str, str]:
    """Index catalog binaries by source, preserving the established last-wins order."""
    return {
        binary: source
        for source, item in packages.items()
        for binary in item["binaries"]
    }


def _source_dependencies(package: dict, providers: dict[str, str]) -> set[str]:
    """Map declared binary alternatives to known OpenStack source producers."""
    return {
        providers[binary]
        for binary in dependency_names(package["build_depends"])
        if binary in providers
    }


def _expand_build_closure(
    packages: dict[str, dict], candidates: dict[str, dict], providers: dict[str, str]
) -> None:
    """Add independent producers transitively until the selected set stabilizes."""
    while True:
        required: set[str] = set()
        for package in packages.values():
            required.update(_source_dependencies(package, providers))

        additions = required - packages.keys()
        if not additions:
            return

        packages.update({source: candidates[source] for source in additions})


@dataclass
class CatalogBuilder:
    """Join release membership to selected archive records for one target."""

    archives: ArchiveSelection
    series: str
    codename: str
    suite: str
    status: str
    comparison_required: bool
    packages: dict[str, dict] = dataclass_field(default_factory=dict)
    exclusions: list[dict] = dataclass_field(default_factory=list)

    def record(self, name: str, metadata: dict, source: str, membership: str) -> dict:
        """Construct a package record with this target's shared selection policy."""
        return package_record(
            name,
            metadata,
            self.archives.sources[source],
            series=self.series,
            membership=membership,
            series_status=self.status,
            codename=self.codename,
            suite=self.suite,
            comparison_archive=self.archives.comparison_sources.get(source),
            comparison_required=self.comparison_required,
        )

    def select_cycle(self, cycle: dict[str, dict]) -> None:
        """Select verified cycle packages and record every unmapped deliverable."""
        if not cycle:
            raise ValueError(f"No deliverables for {self.codename}")

        for name, metadata in cycle.items():
            source = source_name(name, self.archives.sources)
            if source:
                self.packages[source] = self.record(name, metadata, source, "cycle")
            else:
                self.exclusions.append(
                    {
                        "deliverable": name,
                        "release_type": metadata.get("type"),
                        "repositories": repository_names(metadata),
                        "reason": f"No source package with verified OpenStack Homepage/VCS identity in Ubuntu {self.suite} source indexes",
                    }
                )

    def independent_candidates(self, independent: dict[str, dict]) -> dict[str, dict]:
        """Construct independent candidates without replacing cycle members."""
        candidates = {}
        for name, metadata in independent.items():
            source = source_name(name, self.archives.sources)
            if source and source not in self.packages:
                candidates[source] = self.record(
                    name, metadata, source, "independent-build-dependency"
                )

        return candidates

    def add_dependencies(self, independent: dict[str, dict]) -> None:
        """Complete membership and annotate producer edges without self-dependencies."""
        candidates = self.independent_candidates(independent)
        providers = _binary_providers({**candidates, **self.packages})
        _expand_build_closure(self.packages, candidates, providers)
        for source, item in self.packages.items():
            item.update(UPSTREAM_OVERRIDES.get(source, {}))
            dependencies = _source_dependencies(item, providers) & self.packages.keys()
            item["build_dependencies"] = sorted(dependencies - {source})

    def catalog(self, releases: Path, target: dict | None) -> dict:
        """Serialize selected packages alongside their release and archive provenance."""
        revision = subprocess.check_output(
            ["git", "-C", str(releases), "rev-parse", "HEAD"], text=True
        ).strip()
        return {
            "schema_version": 1,
            "series": self.series,
            "codename": self.codename,
            "series_status": self.status,
            "suite": self.suite,
            "target": target,
            "release_metadata": {
                "repository": "https://opendev.org/openstack/releases",
                "sha": revision,
            },
            "archive_indexes": self.archives.indexes,
            "archive_python_indexes": self.archives.indexes,
            "archive_python_packages": archive_python_packages(self.archives.sources),
            "scope": "All cycle deliverables mapped to Ubuntu main/universe source packages, plus transitive independently released OpenStack build dependencies",
            "packages": sorted(self.packages.values(), key=lambda item: item["source"]),
            "exclusions": self.exclusions,
        }


def make_catalog(
    releases: Path,
    source_indexes: list[Path],
    *,
    series: str = "2026.2",
    codename: str = "hibiscus",
    suite: str = "resolute",
    target: dict | None = None,
    index_metadata: dict[str, dict] | None = None,
) -> dict:
    """Assemble a catalog from archive selection, release membership, and build closure."""
    metadata = index_metadata or {}
    archives = ArchiveSelection()
    for path in source_indexes:
        archives.add_index(path, suite, metadata.get(path.name, {}))

    comparison_required = bool(
        archives.comparison_sources
        or any(item.get("comparison_priority") for item in metadata.values())
    )
    builder = CatalogBuilder(
        archives,
        series,
        codename,
        suite,
        _series_status(releases, codename, series),
        comparison_required,
    )
    builder.select_cycle(_deliverables(releases, codename))
    builder.add_dependencies(_deliverables(releases, "_independent"))
    return builder.catalog(releases, target)


def write_catalog(catalog: dict, destination: Path) -> None:
    """Write an indented JSON catalog and create its output directory as needed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(catalog, indent=2) + "\n")


def _download_binary_index(spec: dict, destination: Path, opener: Callable[..., Any]) -> Path:
    """Atomically download one target binary index, failing closed on unavailable archives."""
    path = destination / spec['filename']
    temporary = path.with_suffix('.part')
    with opener(Request(spec['url'], headers={'User-Agent': 'packagetest-catalog/1'}), timeout=90) as response:
        temporary.write_bytes(response.read())

    temporary.replace(path)
    return path


def _retain_binary(records: dict[str, dict], item: dict, spec: dict) -> None:
    """Retain the highest Debian binary version across equally prioritized target archives."""
    name, version = item['Package'], item['Version']
    previous = records.get(name)
    if previous and subprocess.run(['dpkg', '--compare-versions', version, 'le', previous['version']]).returncode == 0:
        return

    records[name] = dict(binary=name, source=item.get('Source', name).split()[0],
                         version=version, architecture=item['Architecture'], archive_index=spec['filename'])


def _python_binary_records(records: list[dict]) -> list[dict]:
    """Expose installed Python distribution providers from actual published binaries."""
    return [dict(item, distribution=re.sub(r'[-_.]+', '-', item['binary'].removeprefix('python3-')).lower())
            for item in records if re.fullmatch(r'python3-[a-z0-9][a-z0-9+.-]*', item['binary'])]


def populate_binary_catalog(catalog: dict, destination: Path, *, opener: Callable[..., Any] = urlopen) -> None:
    """Replace inferred binary availability with target-specific published package versions."""
    target = catalog.get('target') or {'suite': catalog['suite'], 'architecture': 'amd64'}
    destination.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict] = {}
    indexes = []
    for spec in binary_indexes(target):
        path = _download_binary_index(spec, destination, opener)
        data = path.read_bytes()
        indexes.append(dict(spec, sha256=hashlib.sha256(data).hexdigest()))
        for item in paragraphs(gzip.decompress(data).decode()):
            _retain_binary(records, item, spec)

    catalog['archive_binary_packages'] = [records[name] for name in sorted(records)]
    catalog['archive_binary_indexes'] = indexes
    catalog['archive_python_indexes'] = indexes
    catalog['archive_python_packages'] = _python_binary_records(catalog['archive_binary_packages'])
