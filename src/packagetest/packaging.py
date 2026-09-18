from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shlex import quote

from .models import PackageDefinition
from .versioning import upstream_version_to_debian_version


@dataclass(frozen=True)
class PackageOperationPlan:
    source_package: str
    packaging_branch: str | None
    upstream_ref: str
    upstream_version: str
    generated_debian_version: str
    workspace_dir: Path
    packaging_checkout_dir: Path
    upstream_checkout_dir: Path
    orig_tarball: Path
    packaging_branch_file: Path


def _orig_tarball_basename(source_package: str) -> str:
    if source_package.startswith("python-"):
        return source_package.removeprefix("python-")
    return source_package


def build_package_operation_plan(
    *,
    package: PackageDefinition,
    upstream_ref: str,
    upstream_version: str,
    ubuntu_release: str,
    run_dir: Path,
) -> PackageOperationPlan:
    packaging_branch = package.packaging_branch
    workspace_dir = run_dir / package.source_package
    packaging_checkout_dir = workspace_dir / "packaging"
    upstream_checkout_dir = workspace_dir / "upstream"
    return PackageOperationPlan(
        source_package=package.source_package,
        packaging_branch=packaging_branch,
        upstream_ref=upstream_ref,
        upstream_version=upstream_version,
        generated_debian_version=upstream_version_to_debian_version(upstream_version),
        workspace_dir=workspace_dir,
        packaging_checkout_dir=packaging_checkout_dir,
        upstream_checkout_dir=upstream_checkout_dir,
        orig_tarball=workspace_dir / f"{_orig_tarball_basename(package.source_package)}_{upstream_version}.orig.tar.gz",
        packaging_branch_file=workspace_dir / "packaging-branch.txt",
    )


def package_operation_commands(
    *,
    package: PackageDefinition,
    operation_plan: PackageOperationPlan,
    ubuntu_release: str,
    dependency_repository_paths: list[Path] | None = None,
) -> list[tuple[list[str], Path]]:
    packaging_checkout_q = quote(str(operation_plan.packaging_checkout_dir))
    packaging_branch_file_q = quote(str(operation_plan.packaging_branch_file))
    archive_prefix = f"{_orig_tarball_basename(package.source_package)}-{operation_plan.upstream_version}/"
    dependency_repository_paths = dependency_repository_paths or []
    if operation_plan.packaging_branch is None:
        resolve_packaging_branch_cmd = (
            "branch=$(git ls-remote --symref "
            f"{quote(package.packaging_repo)} HEAD | "
            "sed -n 's#^ref: refs/heads/\\([^[:space:]]*\\)[[:space:]]*HEAD$#\\1#p' | head -n1); "
            "if [ -z \"$branch\" ]; then echo 'Unable to determine packaging default branch' >&2; exit 1; fi; "
            f"printf '%s\\n' \"$branch\" > {packaging_branch_file_q}"
        )
    else:
        resolve_packaging_branch_cmd = (
            f"printf '%s\\n' {quote(operation_plan.packaging_branch)} > {packaging_branch_file_q}"
        )
    checkout_packaging_branch_cmd = (
        f"branch=$(cat {packaging_branch_file_q}); "
        f"git -C {packaging_checkout_q} checkout \"$branch\""
    )
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
    extra_repo_args = " ".join(
        f"--extra-repository={quote(f'deb [trusted=yes] file://{repo.resolve()} {ubuntu_release} main')}"
        for repo in dependency_repository_paths
    )
    sbuild_command = f"sbuild --dist={quote(ubuntu_release)} --build=source+all+any"
    if extra_repo_args:
        sbuild_command = f"{sbuild_command} {extra_repo_args}"
    sbuild_command = f"{sbuild_command} ../*.dsc"
    gbp_import_orig_cmd = (
        f"branch=$(cat {packaging_branch_file_q}); "
        "gbp import-orig "
        "--upstream-branch=upstream "
        "--pristine-tar "
        "--no-interactive "
        f"--debian-branch=\"$branch\" "
        f"--upstream-version={quote(operation_plan.upstream_version)} "
        f"{quote(str(operation_plan.orig_tarball))}"
    )
    gbp_buildpackage_cmd = (
        f"branch=$(cat {packaging_branch_file_q}); "
        "gbp buildpackage "
        "--git-upstream-branch=upstream "
        "--git-pristine-tar "
        "--git-builder='debuild -S -sa' "
        "--git-debian-branch=\"$branch\""
    )

    return [
        (["rm", "-rf", str(operation_plan.workspace_dir)], operation_plan.workspace_dir.parent),
        (["mkdir", "-p", str(operation_plan.workspace_dir)], operation_plan.workspace_dir.parent),
        (["git", "clone", package.packaging_repo, str(operation_plan.packaging_checkout_dir)], operation_plan.workspace_dir.parent),
        (["bash", "-lc", resolve_packaging_branch_cmd], operation_plan.workspace_dir.parent),
        (["bash", "-lc", checkout_packaging_branch_cmd], operation_plan.workspace_dir.parent),
        (["git", "-C", str(operation_plan.packaging_checkout_dir), "rev-parse", "HEAD"], operation_plan.workspace_dir.parent),
        (
            ["bash", "-lc", upstream_branch_cmd],
            operation_plan.workspace_dir.parent,
        ),
        (
            ["bash", "-lc", pristine_tar_branch_cmd],
            operation_plan.workspace_dir.parent,
        ),
        (
            ["bash", "-lc", checkout_packaging_branch_cmd],
            operation_plan.workspace_dir.parent,
        ),
        (["git", "clone", package.upstream_repo, str(operation_plan.upstream_checkout_dir)], operation_plan.workspace_dir.parent),
        (["git", "-C", str(operation_plan.upstream_checkout_dir), "checkout", operation_plan.upstream_ref], operation_plan.workspace_dir.parent),
        (["bash", "-lc", archive_cmd], operation_plan.workspace_dir.parent),
        (["bash", "-lc", gbp_import_orig_cmd], operation_plan.packaging_checkout_dir),
        (["gbp", "pq", "import"], operation_plan.packaging_checkout_dir),
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
        (["bash", "-lc", gbp_buildpackage_cmd], operation_plan.packaging_checkout_dir),
        (
            [
                "bash",
                "-lc",
                sbuild_command,
            ],
            operation_plan.packaging_checkout_dir,
        ),
    ]


def classify_packaging_failure(command: list[str], stdout: str, stderr: str) -> str:
    command_text = " ".join(command)
    output_lower = "\n".join((stdout, stderr)).lower()
    if "missing origin/upstream branch" in output_lower or "missing origin/pristine-tar branch" in output_lower:
        return "PACKAGING_POLICY_FAILURE"
    if (
        "ls-remote --symref" in command_text
        or (" checkout " in command_text and "did not match any file" in output_lower)
        or "unable to determine packaging default branch" in output_lower
    ):
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
    if "apt-ftparchive" in command_text or command[:1] == ["gpg"] or command[:3] == ["gpgconf", "--kill", "gpg-agent"]:
        return "REPOSITORY_PUBLISH_FAILURE"
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
