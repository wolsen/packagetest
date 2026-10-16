"""Validated identities at the JSON boundary of the active build pipeline."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from .relations import BuildContext


def required_text(value: Mapping[str, object], key: str) -> str:
    """Read a required nonempty string from the JSON boundary."""
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"Expected nonempty string for {key}")

    return item


def _validate_target_names(*names: str) -> None:
    """Reject names unsuitable for Debian target identifiers."""
    for name in names:
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
            raise ValueError(f"Invalid build target field: {name!r}")


def _build_profiles(value: Mapping[str, object]) -> list[str]:
    """Read profile names without accepting arbitrary JSON collections."""
    profiles = value.get("build_profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
        raise ValueError("build_profiles must be a list of profile names")

    return profiles


def _validate_digests(*digests: str) -> None:
    """Require canonical lowercase SHA-256 identities."""
    for digest in digests:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid prepared source checksum")


@dataclass(frozen=True)
class BuildTarget:
    """Validated distribution and build-context identity."""

    suite: str
    architecture: str
    distribution: str
    profiles: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BuildTarget:
        """Construct a target from its serialized identity."""
        suite = required_text(value, "suite")
        architecture = required_text(value, "architecture")
        distribution = (
            required_text(value, "distribution") if "distribution" in value else suite
        )
        _validate_target_names(suite, architecture, distribution)
        profiles = _build_profiles(value)
        context = BuildContext(architecture, frozenset(profiles))
        return cls(suite, architecture, distribution, context.profiles)

    @property
    def context(self) -> BuildContext:
        """Return the validated dependency evaluation context."""
        return BuildContext(self.architecture, self.profiles)


@dataclass(frozen=True)
class PreparedSource:
    """Identity only; large diagnostic records remain JSON at the reporting edge."""

    source: str
    version: str
    dsc: str
    dsc_sha256: str
    catalog_entry_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PreparedSource:
        """Validate and construct a prepared source identity."""
        schema = value.get("schema_version")
        if type(schema) is not int or schema != 1:
            raise ValueError("Unsupported prepared source schema_version")

        source = cls(
            *(
                required_text(value, key)
                for key in (
                    "source",
                    "version",
                    "dsc",
                    "dsc_sha256",
                    "catalog_entry_sha256",
                )
            )
        )
        source._validate_identity()
        return source

    def _validate_identity(self) -> None:
        """Validate the source name, bundle-relative filename, and digests."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", self.source):
            raise ValueError("Invalid prepared source name")

        if Path(self.dsc).name != self.dsc or not self.dsc.endswith(".dsc"):
            raise ValueError(
                "Prepared .dsc must be a filename within its source bundle"
            )

        _validate_digests(self.dsc_sha256, self.catalog_entry_sha256)

    def verified_path(self, directory: Path) -> Path:
        """Reject symlinked paths before any artifact bytes are trusted."""
        path = directory / self.dsc
        if directory.is_symlink() or path.is_symlink():
            raise ValueError("Symlink in prepared source path")

        return path


@dataclass
class RemediationSession:
    """Own attempt budgets independently of CLI arguments and subprocesses."""

    deadline: float
    max_model_calls: int
    max_rebuilds: int
    max_repairs: int
    model_calls: int = 0
    rebuilds: int = 0

    def stop_reason(self, now: float, repairs: int) -> str | None:
        """Return the first exhausted budget, in enforcement order."""
        if self.model_calls >= self.max_model_calls:
            return "model-call budget exhausted"

        if self.rebuilds >= self.max_rebuilds:
            return "rebuild budget exhausted"

        if repairs >= self.max_repairs:
            return "validated repair budget exhausted"

        if now >= self.deadline:
            return "remediation wall-clock budget exhausted"

        return None

    def timeout(self, requested: int, now: float) -> int:
        """Bound a subprocess timeout by the remaining wall-clock budget."""
        return max(1, min(requested, int(self.deadline - now)))


# Serialized records stay dictionaries so artifact schemas remain stable. Parsers
# validate untrusted JSON before dependency code relies on these field types.


class BinaryMetadata(TypedDict):
    """Dependencies and activation restrictions declared by one binary stanza."""

    package: str
    architecture: str
    depends: str
    pre_depends: str
    recommends: str
    build_profiles: str


class AutopkgtestMetadata(TypedDict):
    """One test stanza and its unexpanded Debian dependency declarations."""

    tests: str
    depends: str
    restrictions: str
    features: str


class ControlMetadata(TypedDict):
    """Exact dependency declarations extracted from prepared Debian control files."""

    build_depends: dict[str, str]
    binary_packages: list[BinaryMetadata]
    autopkgtests: list[AutopkgtestMetadata]


class PreparedMetadata(ControlMetadata):
    """The subset of a prepared manifest used to resolve candidate dependencies."""

    source: str
    version: str
    binaries: list[str]
    testsuite_triggers: str


class SourceArtifact(TypedDict):
    """A bundle-relative source component bound to its SHA-256 digest."""

    file: str
    sha256: str


class PreparedManifest(PreparedMetadata):
    """Version-one prepared-source artifact and its immutable provenance."""

    schema_version: int
    testsuite: str
    dsc: str
    dsc_sha256: str
    source_artifacts: list[SourceArtifact]
    catalog_entry_sha256: str
    pins: dict[str, str | None]
    packaging_source: dict[str, object] | None
    ci: dict[str, str] | None
    artifact_root: NotRequired[str]


def object_mapping(value: object, label: str) -> dict[str, object]:
    """Require a string-keyed object and detach its top-level mapping."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")

    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must have string keys")

    return dict(value)


def object_list(value: object, label: str) -> list[object]:
    """Require a JSON array instead of silently iterating strings or objects."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")

    return list(value)


def string_list(value: object, label: str) -> list[str]:
    """Validate every array member before exposing a list of strings."""
    result = []
    for item in object_list(value, label):
        if not isinstance(item, str):
            raise ValueError(f"{label} must contain only strings")

        result.append(item)

    return result


def string_mapping(value: object, label: str) -> dict[str, str]:
    """Require string values in a JSON object such as folded Debian fields."""
    result = {}
    for key, item in object_mapping(value, label).items():
        if not isinstance(item, str):
            raise ValueError(f"{label}.{key} must be a string")

        result[key] = item

    return result


def text_field(value: Mapping[str, object], key: str, default: str = "") -> str:
    """Read a possibly empty textual declaration without coercing other types."""
    item = value.get(key, default)
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")

    return item


def records(value: object, label: str) -> list[dict[str, object]]:
    """Validate an array of JSON objects while retaining their extension fields."""
    return [object_mapping(item, label) for item in object_list(value, label)]


def binary_metadata(value: Mapping[str, object]) -> BinaryMetadata:
    """Normalize optional binary fields while requiring its package identity."""
    return BinaryMetadata(
        package=required_text(value, "package"),
        architecture=text_field(value, "architecture", "any"),
        depends=text_field(value, "depends"),
        pre_depends=text_field(value, "pre_depends"),
        recommends=text_field(value, "recommends"),
        build_profiles=text_field(value, "build_profiles"),
    )


def autopkgtest_metadata(value: Mapping[str, object]) -> AutopkgtestMetadata:
    """Normalize optional fields in one serialized autopkgtest stanza."""
    return AutopkgtestMetadata(
        tests=text_field(value, "tests", "unnamed"),
        depends=text_field(value, "depends"),
        restrictions=text_field(value, "restrictions"),
        features=text_field(value, "features"),
    )


def prepared_metadata(value: Mapping[str, object]) -> PreparedMetadata:
    """Validate dependency inputs without requiring the bundle's provenance fields."""
    return PreparedMetadata(
        source=required_text(value, "source"),
        version=required_text(value, "version"),
        binaries=string_list(value.get("binaries"), "binaries"),
        build_depends=string_mapping(value.get("build_depends", {}), "build_depends"),
        binary_packages=[
            binary_metadata(item)
            for item in records(value.get("binary_packages", []), "binary_packages")
        ],
        autopkgtests=[
            autopkgtest_metadata(item)
            for item in records(value.get("autopkgtests", []), "autopkgtests")
        ],
        testsuite_triggers=text_field(value, "testsuite_triggers"),
    )


def source_artifact(value: Mapping[str, object]) -> SourceArtifact:
    """Reject path escapes and malformed component digests before filesystem access."""
    name = required_text(value, "file")
    if Path(name).name != name or name in {".", ".."} or "\\" in name:
        raise ValueError("Source artifact must be a bundle-relative filename")

    digest = required_text(value, "sha256")
    _validate_digests(digest)
    return SourceArtifact(file=name, sha256=digest)


def _source_pins(value: object) -> dict[str, str | None]:
    """Read nullable Git pins without accepting arbitrary nested provenance."""
    result: dict[str, str | None] = {}
    for key, item in object_mapping(value, "pins").items():
        if item is not None and not isinstance(item, str):
            raise ValueError(f"pins.{key} must be a string or null")

        result[key] = item

    return result


def prepared_manifest(value: object) -> PreparedManifest:
    """Validate all manifest fields before trusting metadata or source filenames."""
    mapping = object_mapping(value, "prepared source manifest")
    identity = PreparedSource.from_mapping(mapping)
    metadata = prepared_metadata(mapping)
    ci = mapping.get("ci")
    packaging = mapping.get("packaging_source")
    artifacts = [
        source_artifact(item)
        for item in records(mapping.get("source_artifacts"), "source_artifacts")
    ]
    _validate_artifact_names(artifacts)
    return PreparedManifest(
        source=metadata["source"],
        version=metadata["version"],
        binaries=metadata["binaries"],
        build_depends=metadata["build_depends"],
        binary_packages=metadata["binary_packages"],
        autopkgtests=metadata["autopkgtests"],
        testsuite_triggers=metadata["testsuite_triggers"],
        schema_version=1,
        testsuite=text_field(mapping, "testsuite"),
        dsc=identity.dsc,
        dsc_sha256=identity.dsc_sha256,
        catalog_entry_sha256=identity.catalog_entry_sha256,
        source_artifacts=artifacts,
        pins=_source_pins(mapping.get("pins")),
        packaging_source=None
        if packaging is None
        else object_mapping(packaging, "packaging_source"),
        ci=None if ci is None else string_mapping(ci, "ci"),
    )


def _validate_artifact_names(artifacts: list[SourceArtifact]) -> None:
    """Require a nonempty inventory with exactly one record per filename."""
    names = [item["file"] for item in artifacts]
    if not names or len(names) != len(set(names)):
        raise ValueError("Missing or duplicate prepared source artifact names")
