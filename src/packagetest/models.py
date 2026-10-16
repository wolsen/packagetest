from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PackageDefinition:
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
    command: list[str]
    cwd: str
    env_diff: dict[str, str]
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlannedBuild:
    source_package: str
    depends_on_sources: list[str]
    package: PackageDefinition


@dataclass
class BuildPlan:
    generation_id: str
    openstack_target: str
    ubuntu_release: str
    planned_builds: list[PlannedBuild]
    snapshot_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
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
