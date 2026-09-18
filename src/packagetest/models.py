from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class BuildState(str, Enum):
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
    BUILDING = "BUILDING"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_SUCCEEDED = "BUILD_SUCCEEDED"
    PUBLISHED = "PUBLISHED"
    BLOCKED_BY_FAILED_DEPENDENCY = "BLOCKED_BY_FAILED_DEPENDENCY"


@dataclass(frozen=True)
class PackageDefinition:
    source_package: str
    binary_packages: list[str]
    upstream_repo: str
    packaging_repo: str
    build_depends_on_sources: list[str] = field(default_factory=list)
    branch_mapping: dict[str, str] = field(default_factory=dict)
    source_creation_method: str = "opendev-tarball"


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "openstack_target": self.openstack_target,
            "ubuntu_release": self.ubuntu_release,
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


@dataclass
class PackageManifest:
    source_package: str
    upstream_repo: str
    upstream_tag_or_sha: str
    upstream_version: str
    packaging_repo: str
    packaging_branch: str
    packaging_base_sha: str
    generated_debian_version: str
    source_hashes: list[str]
    build_dependency_versions: dict[str, str]
    generated_binary_packages: list[str]
    generated_binary_hashes: list[str]
    runner_environment: dict[str, str]
    build_started_at: str
    build_finished_at: str
    build_result: BuildState


@dataclass
class GenerationManifest:
    generation_id: str
    openstack_release_target: str
    ubuntu_release: str
    package_manifests: list[PackageManifest]

    def as_dict(self) -> dict[str, Any]:
        package_manifests = []
        for package_manifest in self.package_manifests:
            row = asdict(package_manifest)
            row["build_result"] = package_manifest.build_result.value
            package_manifests.append(row)
        return {
            "generation_id": self.generation_id,
            "openstack_release_target": self.openstack_release_target,
            "ubuntu_release": self.ubuntu_release,
            "package_manifests": package_manifests,
        }


@dataclass
class PackageExecutionMetadata:
    upstream_tag_or_sha: str
    upstream_version: str
    packaging_branch: str
    packaging_base_sha: str = "unknown"
    generated_debian_version: str = "unknown"
    source_hashes: list[str] = field(default_factory=list)
    build_dependency_versions: dict[str, str] = field(default_factory=dict)
    generated_binary_hashes: list[str] = field(default_factory=list)
    build_started_at: str = "unknown"
    build_finished_at: str = "unknown"
