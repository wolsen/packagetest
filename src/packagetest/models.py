"""Shared planning records and command results exchanged between execution boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict


class CommandRecord(TypedDict):
    """Serialized command evidence with explicit nullable complete-output paths."""

    command: list[str]
    cwd: str
    env_diff: dict[str, str]
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    stdout_path: str | None
    stderr_path: str | None


class PlannedBuildRecord(TypedDict):
    """Portable package selection exported in a declarative build plan."""

    source_package: str
    depends_on_sources: list[str]
    binary_packages: list[str]
    upstream_repo: str
    packaging_repo: str
    source_creation_method: str


class BuildPlanRecord(TypedDict):
    """Serialized identity and ordered package selections for one generation."""

    generation_id: str
    openstack_target: str
    ubuntu_release: str
    snapshot_at: str | None
    planned_builds: list[PlannedBuildRecord]


@dataclass(frozen=True)
class PackageDefinition:
    """Configuration for one source package and its packaging repositories."""

    source_package: str
    binary_packages: list[str]
    upstream_repo: str
    packaging_repo: str
    build_depends_on_sources: list[str] = field(default_factory=list)
    packaging_branch: str | None = None
    source_creation_method: str = "opendev-tarball"
    openstack_deliverable: str | None = None


@dataclass
class CommandResult:
    """Command identity, bounded diagnostic tails, and optional complete-output files.

    Executors that truncate stdout must supply its complete-output path. In-memory
    executors may omit paths when stdout contains their complete captured output.
    """

    command: list[str]
    cwd: str
    env_diff: dict[str, str]
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    stdout_path: str | None = None
    stderr_path: str | None = None

    @property
    def stdout_tail(self) -> str:
        """Return the bounded stdout diagnostic text without reading the full log."""
        return self.stdout

    def read_stdout(self) -> str:
        """Read complete stdout from its explicit log path or in-memory capture."""
        if self.stdout_path is None:
            return self.stdout

        return Path(self.stdout_path).read_text(encoding="utf-8", errors="replace")

    def to_dict(self) -> CommandRecord:
        """Serialize command identity, diagnostic tails, and complete-output locations."""
        return CommandRecord(
            command=list(self.command),
            cwd=self.cwd,
            env_diff=dict(self.env_diff),
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
            duration_seconds=self.duration_seconds,
            stdout_path=self.stdout_path,
            stderr_path=self.stderr_path,
        )


@dataclass(frozen=True)
class PlannedBuild:
    """One selected package together with the sources that must precede it."""

    source_package: str
    depends_on_sources: list[str]
    package: PackageDefinition


@dataclass
class BuildPlan:
    """Ordered source selections and the target identity for one generation."""

    generation_id: str
    openstack_target: str
    ubuntu_release: str
    planned_builds: list[PlannedBuild]
    snapshot_at: str | None = None

    def as_dict(self) -> BuildPlanRecord:
        """Export the plan using the public generation artifact schema."""
        return {
            "generation_id": self.generation_id,
            "openstack_target": self.openstack_target,
            "ubuntu_release": self.ubuntu_release,
            "snapshot_at": self.snapshot_at,
            "planned_builds": [
                {
                    "source_package": p.source_package,
                    "depends_on_sources": p.depends_on_sources,
                    "binary_packages": p.package.binary_packages,
                    "upstream_repo": p.package.upstream_repo,
                    "packaging_repo": p.package.packaging_repo,
                    "source_creation_method": p.package.source_creation_method,
                }
                for p in self.planned_builds
            ],
        }
