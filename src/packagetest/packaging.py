from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shlex import quote

from .models import PackageDefinition
from .versioning import openstack_target_to_debian_version, openstack_target_to_upstream_version


@dataclass(frozen=True)
class PackageOperationPlan:
    source_package: str
    packaging_branch: str
    upstream_ref: str
    upstream_version: str
    generated_debian_version: str
    workspace_dir: Path
    packaging_checkout_dir: Path
    upstream_checkout_dir: Path
    orig_tarball: Path


def build_package_operation_plan(
    *,
    package: PackageDefinition,
    openstack_target: str,
    ubuntu_release: str,
    run_dir: Path,
) -> PackageOperationPlan:
    packaging_branch = package.branch_mapping.get(ubuntu_release)
    if packaging_branch is None:
        raise ValueError(f"No packaging branch configured for {package.source_package} on Ubuntu release {ubuntu_release}")
    workspace_dir = run_dir / package.source_package
    packaging_checkout_dir = workspace_dir / "packaging"
    upstream_checkout_dir = workspace_dir / "upstream"
    upstream_version = openstack_target_to_upstream_version(openstack_target)
    return PackageOperationPlan(
        source_package=package.source_package,
        packaging_branch=packaging_branch,
        upstream_ref=openstack_target,
        upstream_version=upstream_version,
        generated_debian_version=openstack_target_to_debian_version(openstack_target),
        workspace_dir=workspace_dir,
        packaging_checkout_dir=packaging_checkout_dir,
        upstream_checkout_dir=upstream_checkout_dir,
        orig_tarball=workspace_dir / f"{package.source_package}_{upstream_version}.orig.tar.gz",
    )


def package_operation_commands(
    *,
    package: PackageDefinition,
    operation_plan: PackageOperationPlan,
    ubuntu_release: str,
) -> list[tuple[list[str], Path]]:
    packaging_checkout_q = quote(str(operation_plan.packaging_checkout_dir))
    archive_prefix = f"{package.source_package}-{operation_plan.upstream_version}/"
    archive_cmd = (
        "git -C "
        f"{quote(str(operation_plan.upstream_checkout_dir))} "
        f"archive --format=tar.gz --prefix={quote(archive_prefix)} {quote(operation_plan.upstream_ref)} "
        f"> {quote(str(operation_plan.orig_tarball))}"
    )
    upstream_branch_cmd = (
        f"if git -C {packaging_checkout_q} show-ref --verify --quiet refs/remotes/origin/upstream; then "
        f"git -C {packaging_checkout_q} checkout -B upstream origin/upstream; "
        f"else echo 'Missing origin/upstream branch' >&2; exit 1; fi"
    )
    pristine_tar_branch_cmd = (
        f"if git -C {packaging_checkout_q} show-ref --verify --quiet refs/remotes/origin/pristine-tar; then "
        f"git -C {packaging_checkout_q} checkout -B pristine-tar origin/pristine-tar; "
        f"else echo 'Missing origin/pristine-tar branch' >&2; exit 1; fi"
    )
    return [
        (["rm", "-rf", str(operation_plan.workspace_dir)], operation_plan.workspace_dir.parent),
        (["mkdir", "-p", str(operation_plan.workspace_dir)], operation_plan.workspace_dir.parent),
        (["git", "clone", package.packaging_repo, str(operation_plan.packaging_checkout_dir)], operation_plan.workspace_dir.parent),
        (
            [
                "git",
                "-C",
                str(operation_plan.packaging_checkout_dir),
                "checkout",
                "-B",
                operation_plan.packaging_branch,
                f"origin/{operation_plan.packaging_branch}",
            ],
            operation_plan.workspace_dir.parent,
        ),
        (["git", "rev-parse", "HEAD"], operation_plan.packaging_checkout_dir),
        (
            ["bash", "-lc", upstream_branch_cmd],
            operation_plan.workspace_dir.parent,
        ),
        (
            ["bash", "-lc", pristine_tar_branch_cmd],
            operation_plan.workspace_dir.parent,
        ),
        (
            [
                "git",
                "-C",
                str(operation_plan.packaging_checkout_dir),
                "checkout",
                "-B",
                operation_plan.packaging_branch,
                f"origin/{operation_plan.packaging_branch}",
            ],
            operation_plan.workspace_dir.parent,
        ),
        (["git", "clone", package.upstream_repo, str(operation_plan.upstream_checkout_dir)], operation_plan.workspace_dir.parent),
        (["git", "-C", str(operation_plan.upstream_checkout_dir), "checkout", operation_plan.upstream_ref], operation_plan.workspace_dir.parent),
        (["bash", "-lc", archive_cmd], operation_plan.workspace_dir.parent),
        (
            [
                "gbp",
                "import-orig",
                f"--debian-branch={operation_plan.packaging_branch}",
                "--upstream-branch=upstream",
                "--pristine-tar",
                "--no-interactive",
                f"--upstream-version={operation_plan.upstream_version}",
                str(operation_plan.orig_tarball),
            ],
            operation_plan.packaging_checkout_dir,
        ),
        (["gbp", "pq", "import"], operation_plan.packaging_checkout_dir),
        (
            [
                "git",
                "-C",
                str(operation_plan.packaging_checkout_dir),
                "checkout",
                "-B",
                operation_plan.packaging_branch,
                f"origin/{operation_plan.packaging_branch}",
            ],
            operation_plan.workspace_dir.parent,
        ),
        (
            [
                "dch",
                "--distribution",
                ubuntu_release,
                "--newversion",
                operation_plan.generated_debian_version,
                f"Automated OpenStack package update to {operation_plan.upstream_version}",
            ],
            operation_plan.packaging_checkout_dir,
        ),
        (
            [
                "gbp",
                "buildpackage",
                f"--git-debian-branch={operation_plan.packaging_branch}",
                "--git-upstream-branch=upstream",
                "--git-pristine-tar",
                "--git-builder=debuild -S -sa",
            ],
            operation_plan.packaging_checkout_dir,
        ),
        (
            [
                "bash",
                "-lc",
                f"sbuild --dist={quote(ubuntu_release)} --build=source,all,any ../*.dsc",
            ],
            operation_plan.packaging_checkout_dir,
        ),
    ]


def classify_packaging_failure(command: list[str], stdout: str, stderr: str) -> str:
    command_text = " ".join(command)
    output_lower = "\n".join((stdout, stderr)).lower()
    if "missing origin/upstream branch" in output_lower or "missing origin/pristine-tar branch" in output_lower:
        return "PACKAGING_POLICY_FAILURE"
    if "gbp pq import" in command_text:
        if any(
            marker in output_lower
            for marker in ("patch failed", "patch does not apply", "quilt", "merge conflict", "cannot apply")
        ):
            return "PATCH_APPLY_FAILURE"
        return "PACKAGING_POLICY_FAILURE"
    if "gbp import-orig" in command_text or " archive --format=tar.gz " in command_text:
        return "SOURCE_GENERATION_FAILURE"
    if "apt-ftparchive" in command_text or command[:1] == ["gpg"] or "gpgconf --kill gpg-agent" in command_text:
        return "SOURCE_GENERATION_FAILURE"
    if "sbuild" in command_text:
        if "unmet build dependency" in output_lower or "build dependency" in output_lower:
            return "MISSING_BUILD_DEPENDENCY"
        if "version conflict" in output_lower:
            return "DEPENDENCY_VERSION_CONFLICT"
        if "test suite failure" in output_lower or "pytest" in output_lower:
            return "UNIT_TEST_FAILURE"
        return "COMPILATION_FAILURE"
    if "gbp buildpackage" in command_text or "dch" in command_text:
        return "PACKAGING_POLICY_FAILURE"
    return "UNKNOWN"
