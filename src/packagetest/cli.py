from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from .commands import CommandRunner
from .config import load_package_definitions
from .failures import FailureBundle, write_failure_bundle
from .manifest import write_generation_manifest
from .models import BuildPlan, BuildState, CommandResult, GenerationManifest, PackageExecutionMetadata, PackageManifest
from .packaging import PackageOperationPlan, build_package_operation_plan, classify_packaging_failure, package_operation_commands
from .planner import build_plan, initial_states
from .release_discovery import OpenStackReleaseResolver, ReleaseDiscoveryError, ResolvedRelease
from .repository import apt_repository_commands
from .scheduler import mark_state, next_ready_packages
from .versioning import openstack_target_to_upstream_version, upstream_version_to_debian_version

SOURCE_ARTIFACT_PATTERNS = ("*.dsc", "*.orig.tar.*", "*.debian.tar.*", "*.changes", "*.buildinfo")
BINARY_ARTIFACT_PATTERNS = ("*.deb", "*.udeb", "*.ddeb")


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "vertical_slice.json"


def plan_cmd(args: argparse.Namespace) -> int:
    definitions = load_package_definitions(Path(args.config))
    plan = build_plan(
        definitions=definitions,
        requested_sources=args.sources,
        openstack_target=args.openstack_target,
        ubuntu_release=args.ubuntu_release,
        include_dependency_closure=not args.no_dependency_closure,
        snapshot_at=args.snapshot_at,
    )
    resolved_releases, resolution_errors = _resolve_package_releases(plan, args)
    payload = plan.as_dict()
    for row in payload["planned_builds"]:
        source = row["source_package"]
        resolved_release = resolved_releases.get(source)
        if resolved_release is not None:
            row["resolved_upstream_version"] = resolved_release.version
            row["resolved_upstream_tag_or_sha"] = resolved_release.upstream_ref
            if resolved_release.snapshot_at:
                row["resolved_upstream_sha"] = resolved_release.upstream_ref
            row["release_series"] = resolved_release.series
            row["release_deliverable_path"] = resolved_release.deliverable_path
        if source in resolution_errors:
            row["release_resolution_error"] = resolution_errors[source]
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if resolution_errors else 0


def build_cmd(args: argparse.Namespace) -> int:
    definitions = load_package_definitions(Path(args.config))
    plan = build_plan(
        definitions=definitions,
        requested_sources=args.sources,
        openstack_target=args.openstack_target,
        ubuntu_release=args.ubuntu_release,
        include_dependency_closure=not args.no_dependency_closure,
        snapshot_at=args.snapshot_at,
    )

    run_dir = Path(args.run_dir) / plan.generation_id
    logs_dir = run_dir / "logs"
    artifacts_dir = run_dir / "artifacts"
    logs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dependency_repository_dirs = _discover_dependency_repository_dirs(args.dependency_repo)

    states = initial_states(plan)
    resolved_releases, release_resolution_errors = _resolve_package_releases(plan, args)
    operation_plans: dict[str, PackageOperationPlan] = {}
    operation_plan_errors: dict[str, str] = {}
    package_metadata = {}
    for item in plan.planned_builds:
        resolved_release = resolved_releases.get(item.source_package)
        source_hashes: list[str] = []
        if resolved_release and resolved_release.upstream_ref:
            source_hashes = [f"{'git' if resolved_release.snapshot_at else 'git-ref'}:{resolved_release.upstream_ref}"]
        metadata = PackageExecutionMetadata(
            upstream_tag_or_sha=resolved_release.upstream_ref if resolved_release else plan.openstack_target,
            upstream_version=resolved_release.version if resolved_release else openstack_target_to_upstream_version(plan.openstack_target),
            packaging_branch=item.package.branch_mapping.get(args.ubuntu_release, "unknown"),
            generated_debian_version=upstream_version_to_debian_version(
                resolved_release.version if resolved_release else openstack_target_to_upstream_version(plan.openstack_target)
            ),
            source_hashes=source_hashes,
        )
        package_metadata[item.source_package] = metadata
        if item.source_package in release_resolution_errors:
            operation_plan_errors[item.source_package] = release_resolution_errors[item.source_package]
            continue
        try:
            operation_plans[item.source_package] = build_package_operation_plan(
                package=item.package,
                upstream_ref=metadata.upstream_tag_or_sha,
                upstream_version=metadata.upstream_version,
                ubuntu_release=plan.ubuntu_release,
                run_dir=run_dir,
            )
        except ValueError as exc:
            operation_plan_errors[item.source_package] = str(exc)
        else:
            metadata.packaging_branch = operation_plans[item.source_package].packaging_branch
            metadata.build_output_dir = str(operation_plans[item.source_package].packaging_checkout_dir.parent)

    runner = CommandRunner(log_path=logs_dir / "commands.jsonl")
    while any(state == BuildState.WAITING_FOR_DEPENDENCY for state in states.values()):
        ready = next_ready_packages(plan, states)
        if not ready:
            break
        for source in ready:
            mark_state(states, source, BuildState.BUILDING)
            if source in operation_plan_errors:
                _record_preparation_failure(
                    source,
                    plan,
                    run_dir,
                    states,
                    package_metadata[source],
                    operation_plan_errors[source],
                    _classify_preparation_failure(operation_plan_errors[source]),
                )
                continue
            _run_package(
                source,
                plan,
                args,
                run_dir,
                runner,
                states,
                package_metadata[source],
                operation_plans[source],
                dependency_repository_dirs=dependency_repository_dirs,
            )
    _publish_run_outputs(plan, args, run_dir, runner, states, package_metadata, operation_plans)

    manifests: list[PackageManifest] = []
    for item in plan.planned_builds:
        metadata = package_metadata[item.source_package]
        manifests.append(
            PackageManifest(
                source_package=item.source_package,
                upstream_repo=item.package.upstream_repo,
                upstream_tag_or_sha=metadata.upstream_tag_or_sha,
                upstream_version=metadata.upstream_version,
                packaging_repo=item.package.packaging_repo,
                packaging_branch=metadata.packaging_branch,
                packaging_base_sha=metadata.packaging_base_sha,
                generated_debian_version=metadata.generated_debian_version,
                source_hashes=metadata.source_hashes,
                build_dependency_versions=metadata.build_dependency_versions,
                generated_binary_packages=item.package.binary_packages,
                generated_binary_hashes=metadata.generated_binary_hashes,
                runner_environment={"github_actions": str(bool(Path("/home/runner").exists())).lower()},
                build_started_at=metadata.build_started_at,
                build_finished_at=metadata.build_finished_at,
                build_result=states[item.source_package],
            )
        )

    write_generation_manifest(
        run_dir / "generation-manifest.json",
        GenerationManifest(
            generation_id=plan.generation_id,
            openstack_release_target=plan.openstack_target,
            ubuntu_release=plan.ubuntu_release,
            package_manifests=manifests,
            snapshot_at=plan.snapshot_at,
        ),
    )

    print(json.dumps({"generation_id": plan.generation_id, "states": {k: v.value for k, v in states.items()}}, indent=2))
    return 0


def _run_package(
    source: str,
    plan: BuildPlan,
    args: argparse.Namespace,
    run_dir: Path,
    runner: CommandRunner,
    states: dict[str, BuildState],
    metadata: PackageExecutionMetadata,
    operation_plan: PackageOperationPlan,
    dependency_repository_dirs: list[Path],
) -> None:
    package = next(p.package for p in plan.planned_builds if p.source_package == source)
    metadata.upstream_version = operation_plan.upstream_version
    metadata.generated_debian_version = operation_plan.generated_debian_version
    metadata.packaging_branch = operation_plan.packaging_branch
    metadata.build_output_dir = str(operation_plan.packaging_checkout_dir.parent)
    metadata.build_started_at = datetime.now(UTC).isoformat()

    commands = package_operation_commands(
        package=package,
        operation_plan=operation_plan,
        ubuntu_release=plan.ubuntu_release,
        dependency_repository_paths=dependency_repository_dirs if package.build_depends_on_sources else [],
    )
    for command, cwd in commands:
        planned_command = command
        if args.dry_run:
            command = ["echo", "DRY-RUN:", *command]
        result = runner.run(command=command, cwd=run_dir if args.dry_run else cwd)
        if not args.dry_run and result.exit_code == 0 and planned_command[0:2] == ["git", "-C"] and planned_command[-2:] == ["rev-parse", "HEAD"]:
            metadata.packaging_base_sha = result.stdout.strip() or metadata.packaging_base_sha
        if result.exit_code != 0:
            states[source] = BuildState.BUILD_FAILED
            metadata.build_finished_at = datetime.now(UTC).isoformat()
            write_failure_bundle(
                out_dir=run_dir / "failures" / source,
                bundle=FailureBundle(
                    category=classify_packaging_failure(planned_command, result.stdout, result.stderr),
                    source_package=source,
                    generation_id=plan.generation_id,
                    upstream_sha=None,
                    packaging_sha=metadata.packaging_base_sha if metadata.packaging_base_sha != "unknown" else None,
                    failed_command=result.command,
                    command_exit_code=result.exit_code,
                ),
                command_result=result,
                files={
                    "debian/control": "",
                    "debian/rules": "",
                    "debian/changelog": "",
                    "debian/patches/series": "",
                },
            )
            return

    if not args.dry_run and not _package_has_publishable_outputs(operation_plan.packaging_checkout_dir.parent):
        states[source] = BuildState.BUILD_FAILED
        metadata.build_finished_at = datetime.now(UTC).isoformat()
        result = CommandResult(
            command=["verify-build-output"],
            cwd=str(operation_plan.packaging_checkout_dir.parent),
            env_diff={},
            stdout="",
            stderr="Expected source package artifact (*.dsc) was not produced.",
            exit_code=1,
            duration_seconds=0.0,
        )
        write_failure_bundle(
            out_dir=run_dir / "failures" / source,
            bundle=FailureBundle(
                category="SOURCE_GENERATION_FAILURE",
                source_package=source,
                generation_id=plan.generation_id,
                upstream_sha=None,
                packaging_sha=metadata.packaging_base_sha if metadata.packaging_base_sha != "unknown" else None,
                failed_command=result.command,
                command_exit_code=result.exit_code,
            ),
            command_result=result,
            files={
                "debian/control": "",
                "debian/rules": "",
                "debian/changelog": "",
                "debian/patches/series": "",
            },
        )
        return
    if not args.dry_run:
        metadata.generated_binary_hashes = _compute_binary_hashes(operation_plan.packaging_checkout_dir.parent)
        _stage_package_artifacts(
            source=source,
            output_dir=operation_plan.packaging_checkout_dir.parent,
            run_dir=run_dir,
        )
    metadata.build_finished_at = datetime.now(UTC).isoformat()
    states[source] = BuildState.BUILD_SUCCEEDED


def _record_preparation_failure(
    source: str,
    plan: BuildPlan,
    run_dir: Path,
    states: dict[str, BuildState],
    metadata: PackageExecutionMetadata,
    message: str,
    category: str,
) -> None:
    metadata.build_started_at = datetime.now(UTC).isoformat()
    metadata.build_finished_at = datetime.now(UTC).isoformat()
    states[source] = BuildState.BUILD_FAILED
    result = CommandResult(
        command=["plan-package-operation"],
        cwd=str(run_dir),
        env_diff={},
        stdout="",
        stderr=message,
        exit_code=1,
        duration_seconds=0.0,
    )
    write_failure_bundle(
        out_dir=run_dir / "failures" / source,
        bundle=FailureBundle(
            category=category,
            source_package=source,
            generation_id=plan.generation_id,
            upstream_sha=None,
            packaging_sha=None,
            failed_command=result.command,
            command_exit_code=result.exit_code,
        ),
        command_result=result,
        files={
            "debian/control": "",
            "debian/rules": "",
            "debian/changelog": "",
            "debian/patches/series": "",
        },
    )


def _classify_preparation_failure(message: str) -> str:
    if "No packaging branch configured" in message:
        return "PACKAGING_POLICY_FAILURE"
    return "SOURCE_GENERATION_FAILURE"


def _publish_run_outputs(
    plan: BuildPlan,
    args: argparse.Namespace,
    run_dir: Path,
    runner: CommandRunner,
    states: dict[str, BuildState],
    package_metadata: dict[str, PackageExecutionMetadata],
    operation_plans: dict[str, PackageOperationPlan],
) -> None:
    if not states:
        return
    publishable_sources = [
        source
        for source, state in states.items()
        if state == BuildState.BUILD_SUCCEEDED
        and source in operation_plans
        and _package_has_publishable_outputs(operation_plans[source].packaging_checkout_dir.parent)
    ]
    if not publishable_sources:
        return

    for source in publishable_sources:
        states[source] = BuildState.PUBLISHING

    publish_dir = run_dir / "apt-repo"
    publish_dir.mkdir(parents=True, exist_ok=True)
    pool_dir = publish_dir / "pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    for source in publishable_sources:
        source_output_dir = operation_plans[source].packaging_checkout_dir.parent
        _copy_binary_artifacts_to_pool(source_output_dir, pool_dir)
    for planned_command in apt_repository_commands(publish_dir, plan.ubuntu_release):
        command = ["echo", "DRY-RUN:", *planned_command] if args.dry_run else planned_command
        result = runner.run(command=command, cwd=run_dir if args.dry_run else publish_dir)
        if result.exit_code != 0:
            for source in publishable_sources:
                states[source] = BuildState.PUBLISH_FAILED
                package_metadata[source].build_finished_at = datetime.now(UTC).isoformat()
                _write_package_failure(
                    source=source,
                    plan=plan,
                    run_dir=run_dir,
                    metadata=package_metadata[source],
                    result=result,
                    category=classify_packaging_failure(planned_command, result.stdout, result.stderr),
                )
            return

    for source in publishable_sources:
        states[source] = BuildState.PUBLISHED
        package_metadata[source].build_finished_at = datetime.now(UTC).isoformat()


def _write_package_failure(
    *,
    source: str,
    plan: BuildPlan,
    run_dir: Path,
    metadata: PackageExecutionMetadata,
    result: CommandResult,
    category: str,
) -> None:
    write_failure_bundle(
        out_dir=run_dir / "failures" / source,
        bundle=FailureBundle(
            category=category,
            source_package=source,
            generation_id=plan.generation_id,
            upstream_sha=None,
            packaging_sha=metadata.packaging_base_sha if metadata.packaging_base_sha != "unknown" else None,
            failed_command=result.command,
            command_exit_code=result.exit_code,
        ),
        command_result=result,
        files={
            "debian/control": "",
            "debian/rules": "",
            "debian/changelog": "",
            "debian/patches/series": "",
        },
    )


def _discover_dependency_repository_dirs(raw_paths: list[str]) -> list[Path]:
    discovered: list[Path] = []
    for raw_path in raw_paths:
        root = Path(raw_path)
        if not root.exists():
            continue
        candidates = [root]
        candidates.extend(path for path in root.rglob("apt-repo") if path.is_dir())
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            if (candidate / "Release").exists() and (candidate / "Packages").exists():
                resolved = candidate.resolve()
                if resolved not in discovered:
                    discovered.append(resolved)
    return discovered


def _iter_artifacts(output_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    artifacts: list[Path] = []
    for pattern in patterns:
        artifacts.extend(sorted(output_dir.glob(pattern)))
    return sorted({path.resolve(): path for path in artifacts}.values(), key=lambda path: path.name)


def _copy_binary_artifacts_to_pool(output_dir: Path, pool_dir: Path) -> None:
    for binary_artifact in _iter_artifacts(output_dir, BINARY_ARTIFACT_PATTERNS):
        shutil.copy2(binary_artifact, pool_dir / binary_artifact.name)


def _compute_binary_hashes(output_dir: Path) -> list[str]:
    hashes: list[str] = []
    for binary_artifact in _iter_artifacts(output_dir, BINARY_ARTIFACT_PATTERNS):
        digest = hashlib.sha256(binary_artifact.read_bytes()).hexdigest()
        hashes.append(f"sha256:{digest}")
    return hashes


def _stage_package_artifacts(*, source: str, output_dir: Path, run_dir: Path) -> None:
    source_artifacts = _iter_artifacts(output_dir, SOURCE_ARTIFACT_PATTERNS)
    binary_artifacts = _iter_artifacts(output_dir, BINARY_ARTIFACT_PATTERNS)
    destination_root = run_dir / "artifacts" / source
    destination_source_dir = destination_root / "source"
    destination_binary_dir = destination_root / "binary"
    destination_source_dir.mkdir(parents=True, exist_ok=True)
    destination_binary_dir.mkdir(parents=True, exist_ok=True)
    for artifact in source_artifacts:
        shutil.copy2(artifact, destination_source_dir / artifact.name)
    for artifact in binary_artifacts:
        shutil.copy2(artifact, destination_binary_dir / artifact.name)


def _package_has_publishable_outputs(output_dir: Path) -> bool:
    if not output_dir.exists():
        return False
    return any(output_dir.glob("*.dsc"))


def _resolve_package_releases(
    plan: BuildPlan,
    args: argparse.Namespace,
) -> tuple[dict[str, ResolvedRelease], dict[str, str]]:
    resolver = OpenStackReleaseResolver()
    resolved_releases: dict[str, ResolvedRelease] = {}
    errors: dict[str, str] = {}
    max_workers = min(4, max(1, len(plan.planned_builds)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                resolver.resolve,
                package=item.package,
                openstack_target=plan.openstack_target,
                snapshot_at=args.snapshot_at,
            ): item.source_package
            for item in plan.planned_builds
        }
        for future in as_completed(futures):
            source_package = futures[future]
            try:
                resolved_releases[source_package] = future.result()
            except ReleaseDiscoveryError as exc:
                errors[source_package] = str(exc)
    return resolved_releases, errors


def status_cmd(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"Manifest not found: {manifest}", file=sys.stderr)
        return 1
    print(manifest.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="packaging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--config", default=str(_default_config_path()))
    p_plan.add_argument("--openstack-target", required=True)
    p_plan.add_argument("--ubuntu-release", required=True)
    p_plan.add_argument("--snapshot-at")
    p_plan.add_argument("--no-dependency-closure", action="store_true", default=False)
    p_plan.add_argument("sources", nargs="+")
    p_plan.set_defaults(func=plan_cmd)

    p_build = sub.add_parser("build")
    p_build.add_argument("--config", default=str(_default_config_path()))
    p_build.add_argument("--openstack-target", required=True)
    p_build.add_argument("--ubuntu-release", required=True)
    p_build.add_argument("--snapshot-at")
    p_build.add_argument("--run-dir", default="artifacts")
    p_build.add_argument("--dry-run", action="store_true", default=False)
    p_build.add_argument(
        "--dependency-repo",
        action="append",
        default=[],
        help="Path containing previously published apt-repo artifacts to expose to sbuild.",
    )
    p_build.add_argument(
        "--no-dependency-closure",
        action="store_true",
        default=False,
        help="Build only explicitly requested source packages (used by dependency-layer workflow jobs).",
    )
    p_build.add_argument("sources", nargs="+")
    p_build.set_defaults(func=build_cmd)

    p_status = sub.add_parser("status")
    p_status.add_argument("--manifest", required=True)
    p_status.set_defaults(func=status_cmd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
