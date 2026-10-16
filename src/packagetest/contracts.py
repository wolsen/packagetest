"""Validated identities at the JSON boundary of the active build pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re

from .relations import BuildContext


def _text(value: Mapping[str, object], key: str) -> str:
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
        suite = _text(value, "suite")
        architecture = _text(value, "architecture")
        distribution = (
            _text(value, "distribution") if "distribution" in value else suite
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
        if value.get("schema_version") != 1:
            raise ValueError("Unsupported prepared source schema_version")

        source = cls(
            *(
                _text(value, key)
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
