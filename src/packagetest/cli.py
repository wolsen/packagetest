from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .commands import CommandRunner
from .config import load_package_definitions
from .failures import FailureBundle, write_failure_bundle
from .manifest import write_generation_manifest
from .models import BuildPlan, BuildState, CommandResult, GenerationManifest, PackageExecutionMetadata, PackageManifest
from .packaging import PackageOperationPlan, build_package_operation_plan, classify_packaging_failure, package_operation_commands
from .planner import build_plan, initial_states
from .repository import apt_repository_commands
from .scheduler import mark_state, next_ready_packages
from .versioning import openstack_target_to_debian_version, openstack_target_to_upstream_version


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
    )
    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    return 0


def build_cmd(args: argparse.Namespace) -> int:
    definitions = load_package_definitions(Path(args.config))
    plan = build_plan(
        definitions=definitions,
        requested_sources=args.sources,
        openstack_target=args.openstack_target,
        ubuntu_release=args.ubuntu_release,
        include_dependency_closure=not args.no_dependency_closure,
    )

    run_dir = Path(args.run_dir) / plan.generation_id
    logs_dir = run_dir / "logs"
    artifacts_dir = run_dir / "artifacts"
    logs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    states = initial_states(plan)
    operation_plans: dict[str, PackageOperationPlan] = {}
    operation_plan_errors: dict[str, str] = {}
    package_metadata = {}
    for item in plan.planned_builds:
        metadata = PackageExecutionMetadata(
            upstream_tag_or_sha=plan.openstack_target,
            upstream_version=openstack_target_to_upstream_version(plan.openstack_target),
            packaging_branch=item.package.branch_mapping.get(args.ubuntu_release, "unknown"),
            generated_debian_version=openstack_target_to_debian_version(plan.openstack_target),
        )
        package_metadata[item.source_package] = metadata
        try:
            operation_plans[item.source_package] = build_package_operation_plan(
                package=item.package,
                openstack_target=plan.openstack_target,
                ubuntu_release=plan.ubuntu_release,
                run_dir=run_dir,
            )
        except ValueError as exc:
            operation_plan_errors[item.source_package] = str(exc)
        else:
            metadata.packaging_branch = operation_plans[item.source_package].packaging_branch

    runner = CommandRunner(log_path=logs_dir / "commands.jsonl")
    while any(state == BuildState.WAITING_FOR_DEPENDENCY for state in states.values()):
        ready = next_ready_packages(plan, states)
        if not ready:
            break
        for source in ready:
            mark_state(states, source, BuildState.BUILDING)
            if source in operation_plan_errors:
                _record_operation_plan_failure(source, plan, run_dir, states, package_metadata[source], operation_plan_errors[source])
                continue
            _run_package(source, plan, args, run_dir, runner, states, package_metadata[source], operation_plans[source])

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
) -> None:
    package = next(p.package for p in plan.planned_builds if p.source_package == source)
    metadata.upstream_version = operation_plan.upstream_version
    metadata.generated_debian_version = operation_plan.generated_debian_version
    metadata.packaging_branch = operation_plan.packaging_branch
    metadata.build_started_at = datetime.now(UTC).isoformat()

    commands = package_operation_commands(
        package=package,
        operation_plan=operation_plan,
        ubuntu_release=plan.ubuntu_release,
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

    states[source] = BuildState.BUILD_SUCCEEDED
    if not args.dry_run and not any(operation_plan.workspace_dir.glob("*.dsc")):
        metadata.build_finished_at = datetime.now(UTC).isoformat()
        return
    publish_dir = run_dir / "apt-repo"
    publish_dir.mkdir(parents=True, exist_ok=True)
    publish_commands = [(command, publish_dir) for command in apt_repository_commands(publish_dir, plan.ubuntu_release)]
    states[source] = BuildState.PUBLISHING
    for command, cwd in publish_commands:
        planned_command = command
        if args.dry_run:
            command = ["echo", "DRY-RUN:", *command]
        result = runner.run(command=command, cwd=run_dir if args.dry_run else cwd)
        if result.exit_code != 0:
            states[source] = BuildState.PUBLISH_FAILED
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
    metadata.build_finished_at = datetime.now(UTC).isoformat()
    states[source] = BuildState.PUBLISHED


def _record_operation_plan_failure(
    source: str,
    plan: BuildPlan,
    run_dir: Path,
    states: dict[str, BuildState],
    metadata: PackageExecutionMetadata,
    message: str,
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
            category="PACKAGING_POLICY_FAILURE",
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
    p_plan.add_argument("--no-dependency-closure", action="store_true", default=False)
    p_plan.add_argument("sources", nargs="+")
    p_plan.set_defaults(func=plan_cmd)

    p_build = sub.add_parser("build")
    p_build.add_argument("--config", default=str(_default_config_path()))
    p_build.add_argument("--openstack-target", required=True)
    p_build.add_argument("--ubuntu-release", required=True)
    p_build.add_argument("--run-dir", default="artifacts")
    p_build.add_argument("--dry-run", action="store_true", default=False)
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
