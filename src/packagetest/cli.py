from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .commands import CommandRunner
from .config import load_package_definitions
from .failures import FailureBundle, write_failure_bundle
from .manifest import write_generation_manifest
from .models import BuildPlan, BuildState, GenerationManifest, PackageManifest
from .planner import build_plan
from .repository import apt_repository_commands
from .scheduler import mark_state, next_ready_packages


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "vertical_slice.json"


def plan_cmd(args: argparse.Namespace) -> int:
    definitions = load_package_definitions(Path(args.config))
    plan = build_plan(
        definitions=definitions,
        requested_sources=args.sources,
        openstack_target=args.openstack_target,
        ubuntu_release=args.ubuntu_release,
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
    )

    run_dir = Path(args.run_dir) / plan.generation_id
    logs_dir = run_dir / "logs"
    artifacts_dir = run_dir / "artifacts"
    logs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    states = {b.source_package: BuildState.WAITING_FOR_DEPENDENCY for b in plan.planned_builds}
    for b in plan.planned_builds:
        if not b.depends_on_sources:
            states[b.source_package] = BuildState.BUILDING

    runner = CommandRunner(log_path=logs_dir / "commands.jsonl")
    for source in [b.source_package for b in plan.planned_builds if states[b.source_package] == BuildState.BUILDING]:
        _run_package(source, plan, args, run_dir, runner, states)

    while True:
        ready = next_ready_packages(plan, states)
        if not ready:
            break
        for source in ready:
            mark_state(states, source, BuildState.BUILDING)
            _run_package(source, plan, args, run_dir, runner, states)

    manifests: list[PackageManifest] = []
    for item in plan.planned_builds:
        manifests.append(
            PackageManifest(
                source_package=item.source_package,
                upstream_repo=item.package.upstream_repo,
                upstream_tag_or_sha=args.openstack_target,
                upstream_version="unknown",
                packaging_repo=item.package.packaging_repo,
                packaging_branch=item.package.branch_mapping.get(args.ubuntu_release, "master"),
                packaging_base_sha="unknown",
                generated_debian_version="unknown",
                source_hashes=[],
                build_dependency_versions={},
                generated_binary_packages=item.package.binary_packages,
                generated_binary_hashes=[],
                runner_environment={"github_actions": str(bool(Path("/home/runner").exists())).lower()},
                build_started_at="unknown",
                build_finished_at="unknown",
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
) -> None:
    package = next(p.package for p in plan.planned_builds if p.source_package == source)
    package_dir = run_dir / source
    package_dir.mkdir(parents=True, exist_ok=True)

    commands = [
        ["echo", f"git clone {package.packaging_repo} {source}"],
        ["echo", f"git clone {package.upstream_repo} {source}-upstream"],
        ["echo", f"gbp import-orig --pristine-tar --upstream-version=<resolved> ../{source}_<resolved>.orig.tar.gz"],
        ["echo", "gbp pq import"],
        ["echo", "dch -v <debian-version> \"Automated OpenStack package update\""],
        ["echo", "gbp buildpackage --git-pristine-tar --git-builder='debuild -S -sa'"],
        ["echo", "sbuild --dist=<ubuntu-release> --build=source,all,any ../*.dsc"],
    ]
    commands.extend(apt_repository_commands(run_dir / "apt-repo", plan.ubuntu_release))

    for command in commands:
        if args.dry_run:
            command = ["echo", "DRY-RUN:", *command]
        result = runner.run(command=command, cwd=package_dir)
        if result.exit_code != 0:
            states[source] = BuildState.BUILD_FAILED
            write_failure_bundle(
                out_dir=run_dir / "failures" / source,
                bundle=FailureBundle(
                    category="UNKNOWN",
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
            return

    states[source] = BuildState.BUILD_SUCCEEDED
    states[source] = BuildState.PUBLISHED


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
    p_plan.add_argument("sources", nargs="+")
    p_plan.set_defaults(func=plan_cmd)

    p_build = sub.add_parser("build")
    p_build.add_argument("--config", default=str(_default_config_path()))
    p_build.add_argument("--openstack-target", required=True)
    p_build.add_argument("--ubuntu-release", required=True)
    p_build.add_argument("--run-dir", default="artifacts")
    p_build.add_argument("--dry-run", action="store_true", default=False)
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
