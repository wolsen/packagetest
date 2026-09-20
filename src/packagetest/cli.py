from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import load_package_definitions
from .models import BuildPlan
from .planner import build_plan
from .release_discovery import OpenStackReleaseResolver, ReleaseDiscoveryError, ResolvedRelease
from .locked import LockedBuild, load_lock

logger = logging.getLogger(__name__)

def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "vertical_slice.json"

def plan_cmd(args: argparse.Namespace) -> int:
    logger.debug("Starting plan command with args: %s", vars(args))
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
    logger.debug("Plan command completed with %d release resolution errors", len(resolution_errors))
    return 1 if resolution_errors else 0

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
                logger.debug("Resolved release metadata for %s", source_package)
            except ReleaseDiscoveryError as exc:
                errors[source_package] = str(exc)
                logger.debug("Release resolution failed for %s: %s", source_package, exc)
    return resolved_releases, errors

def status_cmd(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"Manifest not found: {manifest}", file=sys.stderr)
        return 1
    print(manifest.read_text(encoding="utf-8"))
    return 0

def build_cmd(args: argparse.Namespace) -> int:
    try:
        lock = load_lock(Path(args.plan))
        if args.dry_run:
            print(json.dumps({"result": "PLANNED", "lock": lock}, indent=2))
            return 0
        build = LockedBuild(lock, Path(args.run_dir), timeout=args.command_timeout)
        return build.run()
    except (ValueError, KeyError, OSError) as exc:
        logger.error("Build preparation failed: %s", exc)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="packaging")
    _add_logging_flags(parser, default=False)
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="Discover candidate releases; not an executable build lock")
    _add_logging_flags(p_plan, default=argparse.SUPPRESS)
    p_plan.add_argument("--config", default=str(_default_config_path()))
    p_plan.add_argument("--openstack-target", required=True)
    p_plan.add_argument("--ubuntu-release", required=True)
    p_plan.add_argument("--snapshot-at")
    p_plan.add_argument("--no-dependency-closure", action="store_true", default=False)
    p_plan.add_argument("sources", nargs="+")
    p_plan.set_defaults(func=plan_cmd)
    p_build = sub.add_parser("build", help="Build exact inputs from a reviewed schema-v1 lock")
    _add_logging_flags(p_build, default=argparse.SUPPRESS)
    p_build.add_argument("--plan", required=True, help="Path to a schema-v1 build lock")
    p_build.add_argument("--run-dir", default="artifacts")
    p_build.add_argument("--command-timeout", type=float, default=3600)
    p_build.add_argument("--dry-run", action="store_true")
    p_build.set_defaults(func=build_cmd)
    p_status = sub.add_parser("status")
    _add_logging_flags(p_status, default=argparse.SUPPRESS)
    p_status.add_argument("--manifest", required=True)
    p_status.set_defaults(func=status_cmd)
    return parser


def _add_logging_flags(parser: argparse.ArgumentParser, *, default: bool | str) -> None:
    parser.add_argument(
        "--verbose",
        "--debug",
        dest="debug_logging",
        action="store_true",
        default=default,
        help="Enable verbose debug logging, including command stdout/stderr traces.",
    )

def _configure_logging(debug_enabled: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug_enabled else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "debug_logging", False))
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
