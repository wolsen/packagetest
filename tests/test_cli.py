import json
from argparse import Namespace
from pathlib import Path

from packagetest.cli import _discover_dependency_repository_dirs, _publish_run_outputs, _run_package, build_cmd, build_parser, plan_cmd
from packagetest.commands import CommandRunner
from packagetest.config import load_package_definitions
from packagetest.models import BuildState, CommandResult, PackageExecutionMetadata
from packagetest.packaging import build_package_operation_plan
from packagetest.planner import build_plan
from packagetest.release_discovery import ReleaseDiscoveryError, ResolvedRelease


class FailingRunner:
    def __init__(self, *, fail_on: list[str] | None = None, stdout: str = "", stderr: str = "failed") -> None:
        self.calls = 0
        self.fail_on = fail_on
        self.stdout = stdout
        self.stderr = stderr

    def run(self, command, cwd, env_diff=None):
        self.calls += 1
        exit_code = 1 if self.fail_on is None or command == self.fail_on else 0
        stdout = self.stdout if exit_code else ("abc123\n" if command[-2:] == ["rev-parse", "HEAD"] else "")
        stderr = self.stderr if exit_code else ""
        return CommandResult(
            command=command,
            cwd=str(cwd),
            env_diff=env_diff or {},
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_seconds=0.1,
        )


class SuccessfulRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command, cwd, env_diff=None):
        self.calls += 1
        return CommandResult(
            command=command,
            cwd=str(cwd),
            env_diff=env_diff or {},
            stdout="abc123\n" if command[-2:] == ["rev-parse", "HEAD"] else "",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
        )


def test_run_package_dry_run_marks_build_succeeded(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["pbr"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
    )
    states = {"pbr": BuildState.BUILDING}
    args = Namespace(dry_run=True)
    runner = CommandRunner(log_path=tmp_path / "commands.jsonl")
    metadata = PackageExecutionMetadata(
        upstream_tag_or_sha="2027.1-b1",
        upstream_version="unknown",
        packaging_branch="master",
    )
    operation_plan = build_package_operation_plan(
        package=plan.planned_builds[0].package,
        upstream_ref="5.7.0",
        upstream_version="5.7.0",
        ubuntu_release=plan.ubuntu_release,
        run_dir=tmp_path,
    )

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan, dependency_repository_dirs=[])

    assert states["pbr"] == BuildState.BUILD_SUCCEEDED
    assert (tmp_path / "commands.jsonl").exists()
    assert metadata.generated_debian_version == "5.7.0-0ubuntu1"
    assert metadata.build_started_at != "unknown"
    assert metadata.build_finished_at != "unknown"


def test_run_package_without_artifacts_stops_at_build_succeeded(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["pbr"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
    )
    states = {"pbr": BuildState.BUILDING}
    args = Namespace(dry_run=False)
    runner = SuccessfulRunner()
    metadata = PackageExecutionMetadata(
        upstream_tag_or_sha="2027.1-b1",
        upstream_version="unknown",
        packaging_branch="master",
    )
    operation_plan = build_package_operation_plan(
        package=plan.planned_builds[0].package,
        upstream_ref="5.7.0",
        upstream_version="5.7.0",
        ubuntu_release=plan.ubuntu_release,
        run_dir=tmp_path,
    )

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan, dependency_repository_dirs=[])

    assert states["pbr"] == BuildState.BUILD_FAILED
    assert (tmp_path / "failures" / "pbr" / "failure.json").exists()
    assert metadata.build_finished_at != "unknown"


def test_run_package_stages_source_and_binary_artifacts(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["pbr"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
    )
    states = {"pbr": BuildState.BUILDING}
    args = Namespace(dry_run=False)
    runner = SuccessfulRunner()
    metadata = PackageExecutionMetadata(
        upstream_tag_or_sha="2027.1-b1",
        upstream_version="unknown",
        packaging_branch="master",
    )
    operation_plan = build_package_operation_plan(
        package=plan.planned_builds[0].package,
        upstream_ref="5.7.0",
        upstream_version="5.7.0",
        ubuntu_release=plan.ubuntu_release,
        run_dir=tmp_path,
    )
    output_dir = operation_plan.packaging_checkout_dir.parent
    output_dir.mkdir(parents=True)
    (output_dir / "pbr_5.7.0.dsc").write_text("source", encoding="utf-8")
    (output_dir / "python3-pbr_5.7.0_all.deb").write_text("binary", encoding="utf-8")

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan, dependency_repository_dirs=[])

    assert states["pbr"] == BuildState.BUILD_SUCCEEDED
    assert (tmp_path / "artifacts" / "pbr" / "source" / "pbr_5.7.0.dsc").exists()
    assert (tmp_path / "artifacts" / "pbr" / "binary" / "python3-pbr_5.7.0_all.deb").exists()
    assert len(metadata.generated_binary_hashes) == 1


def test_run_package_failure_writes_bundle(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["pbr"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
    )
    states = {"pbr": BuildState.BUILDING}
    args = Namespace(dry_run=False)
    runner = FailingRunner(fail_on=["gbp", "pq", "import"], stdout="patch does not apply", stderr="")
    metadata = PackageExecutionMetadata(
        upstream_tag_or_sha="2027.1-b1",
        upstream_version="unknown",
        packaging_branch="master",
    )
    operation_plan = build_package_operation_plan(
        package=plan.planned_builds[0].package,
        upstream_ref="5.7.0",
        upstream_version="5.7.0",
        ubuntu_release=plan.ubuntu_release,
        run_dir=tmp_path,
    )

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan, dependency_repository_dirs=[])

    assert states["pbr"] == BuildState.BUILD_FAILED
    assert (tmp_path / "failures" / "pbr" / "failure.json").exists()
    failure = json.loads((tmp_path / "failures" / "pbr" / "failure.json").read_text(encoding="utf-8"))
    assert failure["category"] == "PATCH_APPLY_FAILURE"
    assert runner.calls > 1
    assert metadata.build_finished_at != "unknown"


def test_plan_cmd_outputs_release_resolution(capsys, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(plan, args):
        return (
            {
                "pbr": ResolvedRelease(
                    series="indri",
                    release_id="2027.1",
                    version="5.7.0",
                    project_repo="openstack/pbr",
                    project_hash="abc123",
                    upstream_ref="abc123",
                    snapshot_at="2026-09-18T05:32:15+00:00",
                    deliverable_path="deliverables/_independent/pbr.yaml",
                )
            },
            {},
        )

    monkeypatch.setattr("packagetest.cli._resolve_package_releases", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at="2026-09-18T05:32:15+00:00",
        no_dependency_closure=False,
        sources=["pbr"],
    )

    assert plan_cmd(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot_at"] == "2026-09-18T05:32:15+00:00"
    assert payload["planned_builds"][0]["resolved_upstream_version"] == "5.7.0"
    assert payload["planned_builds"][0]["resolved_upstream_tag_or_sha"] == "abc123"


def test_plan_cmd_surfaces_release_resolution_error(capsys, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(plan, args):
        return ({}, {"pbr": "boom"})

    monkeypatch.setattr("packagetest.cli._resolve_package_releases", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at=None,
        no_dependency_closure=False,
        sources=["pbr"],
    )

    assert plan_cmd(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_builds"][0]["release_resolution_error"] == "boom"


def test_build_cmd_records_release_resolution_failure(tmp_path: Path, monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(self, *, package, openstack_target, snapshot_at=None):
        raise ReleaseDiscoveryError("release lookup failed")

    monkeypatch.setattr("packagetest.cli.OpenStackReleaseResolver.resolve", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at=None,
        no_dependency_closure=True,
        run_dir=str(tmp_path),
        dry_run=False,
        dependency_repo=[],
        sources=["pbr"],
    )

    assert build_cmd(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["states"]["pbr"] == "BUILD_FAILED"
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["source_package"] == "pbr"
    assert payload["failures"][0]["category"] == "SOURCE_GENERATION_FAILURE"
    failure_files = list(tmp_path.glob("gen-*/failures/pbr/failure.json"))
    assert len(failure_files) == 1
    failure = json.loads(failure_files[0].read_text(encoding="utf-8"))
    assert failure["category"] == "SOURCE_GENERATION_FAILURE"


def test_build_cmd_skips_publish_without_outputs(tmp_path: Path, monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(plan, args):
        return (
            {
                "pbr": ResolvedRelease(
                    series="indri",
                    release_id="2027.1",
                    version="5.7.0",
                    project_repo="openstack/pbr",
                    project_hash="abc123",
                    upstream_ref="5.7.0",
                    snapshot_at=None,
                    deliverable_path="deliverables/_independent/pbr.yaml",
                )
            },
            {},
        )

    monkeypatch.setattr("packagetest.cli._resolve_package_releases", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at=None,
        no_dependency_closure=True,
        run_dir=str(tmp_path),
        dry_run=True,
        dependency_repo=[],
        sources=["pbr"],
    )

    assert build_cmd(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["states"]["pbr"] == "BUILD_SUCCEEDED"


def test_build_cmd_dry_run_resolves_relative_run_dir_to_absolute(tmp_path: Path, monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(plan, args):
        return (
            {
                "pbr": ResolvedRelease(
                    series="indri",
                    release_id="2027.1",
                    version="5.7.0",
                    project_repo="openstack/pbr",
                    project_hash="abc123",
                    upstream_ref="5.7.0",
                    snapshot_at=None,
                    deliverable_path="deliverables/_independent/pbr.yaml",
                )
            },
            {},
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("packagetest.cli._resolve_package_releases", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at=None,
        no_dependency_closure=True,
        run_dir="artifacts",
        dry_run=True,
        dependency_repo=[],
        sources=["pbr"],
    )

    assert build_cmd(args) == 0
    run_dirs = list((tmp_path / "artifacts").glob("gen-*"))
    assert len(run_dirs) == 1
    command_lines = (run_dirs[0] / "logs" / "commands.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert command_lines
    assert all(Path(json.loads(line)["cwd"]).is_absolute() for line in command_lines)
    payload = json.loads(capsys.readouterr().out)
    assert payload["states"]["pbr"] == "BUILD_SUCCEEDED"


def test_publish_run_outputs_publishes_only_packages_with_outputs(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["pbr", "glance"],
        openstack_target="2027.1",
        ubuntu_release="noble",
        include_dependency_closure=False,
    )
    args = Namespace(dry_run=True)
    runner = CommandRunner(log_path=tmp_path / "commands.jsonl")
    states = {"pbr": BuildState.BUILD_SUCCEEDED, "glance": BuildState.BUILD_SUCCEEDED}
    operation_plans = {
        "pbr": build_package_operation_plan(
            package=definitions["pbr"],
            upstream_ref="5.7.0",
            upstream_version="5.7.0",
            ubuntu_release="noble",
            run_dir=tmp_path,
        ),
        "glance": build_package_operation_plan(
            package=definitions["glance"],
            upstream_ref="31.1.0",
            upstream_version="31.1.0",
            ubuntu_release="noble",
            run_dir=tmp_path,
        ),
    }
    metadata = {
        "pbr": PackageExecutionMetadata(
            upstream_tag_or_sha="5.7.0",
            upstream_version="5.7.0",
            packaging_branch="master",
            build_output_dir=str(tmp_path / "pbr"),
        ),
        "glance": PackageExecutionMetadata(
            upstream_tag_or_sha="31.1.0",
            upstream_version="31.1.0",
            packaging_branch="master",
            build_output_dir=str(tmp_path / "glance"),
        ),
    }
    (tmp_path / "pbr").mkdir(parents=True)
    (tmp_path / "pbr" / "pbr_5.7.0.dsc").write_text("", encoding="utf-8")
    (tmp_path / "pbr" / "python3-pbr_5.7.0_all.deb").write_text("", encoding="utf-8")
    (tmp_path / "artifacts" / "pbr" / "binary").mkdir(parents=True)
    (tmp_path / "artifacts" / "pbr" / "binary" / "python3-pbr_5.7.0_all.deb").write_text("", encoding="utf-8")

    _publish_run_outputs(plan, args, tmp_path, runner, states, metadata, operation_plans)

    assert states["pbr"] == BuildState.PUBLISHED
    assert states["glance"] == BuildState.BUILD_SUCCEEDED
    assert (tmp_path / "apt-repo" / "pool" / "pbr" / "python3-pbr_5.7.0_all.deb").exists()


def test_publish_run_outputs_allows_unrelated_failed_package(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["pbr", "glance"],
        openstack_target="2027.1",
        ubuntu_release="noble",
        include_dependency_closure=False,
    )
    args = Namespace(dry_run=True)
    runner = CommandRunner(log_path=tmp_path / "commands.jsonl")
    states = {"pbr": BuildState.BUILD_SUCCEEDED, "glance": BuildState.BUILD_FAILED}
    operation_plans = {
        "pbr": build_package_operation_plan(
            package=definitions["pbr"],
            upstream_ref="5.7.0",
            upstream_version="5.7.0",
            ubuntu_release="noble",
            run_dir=tmp_path,
        ),
        "glance": build_package_operation_plan(
            package=definitions["glance"],
            upstream_ref="31.1.0",
            upstream_version="31.1.0",
            ubuntu_release="noble",
            run_dir=tmp_path,
        ),
    }
    metadata = {
        "pbr": PackageExecutionMetadata(
            upstream_tag_or_sha="5.7.0",
            upstream_version="5.7.0",
            packaging_branch="master",
            build_output_dir=str(tmp_path / "pbr"),
        ),
        "glance": PackageExecutionMetadata(
            upstream_tag_or_sha="31.1.0",
            upstream_version="31.1.0",
            packaging_branch="master",
            build_output_dir=str(tmp_path / "glance"),
        ),
    }
    (tmp_path / "pbr").mkdir(parents=True)
    (tmp_path / "pbr" / "pbr_5.7.0.dsc").write_text("", encoding="utf-8")
    (tmp_path / "pbr" / "python3-pbr_5.7.0_all.deb").write_text("", encoding="utf-8")
    (tmp_path / "artifacts" / "pbr" / "binary").mkdir(parents=True)
    (tmp_path / "artifacts" / "pbr" / "binary" / "python3-pbr_5.7.0_all.deb").write_text("", encoding="utf-8")

    _publish_run_outputs(plan, args, tmp_path, runner, states, metadata, operation_plans)

    assert states["pbr"] == BuildState.PUBLISHED
    assert states["glance"] == BuildState.BUILD_FAILED
    assert (tmp_path / "apt-repo" / "pool" / "pbr" / "python3-pbr_5.7.0_all.deb").exists()


def test_discover_dependency_repository_dirs_discovers_nested_apt_repo(tmp_path: Path):
    apt_repo = tmp_path / "upstream-generation" / "gen-1" / "apt-repo"
    dists_binary = apt_repo / "dists" / "noble" / "main" / "binary-amd64"
    dists_binary.mkdir(parents=True)
    (apt_repo / "dists" / "noble" / "Release").write_text("", encoding="utf-8")
    (dists_binary / "Packages").write_text("", encoding="utf-8")

    repositories = _discover_dependency_repository_dirs([str(tmp_path / "upstream-generation")])

    assert repositories == [apt_repo.resolve()]


def test_build_parser_supports_verbose_before_subcommand():
    parser = build_parser()
    args = parser.parse_args(
        ["--verbose", "build", "--openstack-target", "2027.1", "--ubuntu-release", "noble", "pbr"]
    )
    assert args.debug_logging is True


def test_build_parser_supports_debug_after_subcommand():
    parser = build_parser()
    args = parser.parse_args(
        ["build", "--debug", "--openstack-target", "2027.1", "--ubuntu-release", "noble", "pbr"]
    )
    assert args.debug_logging is True
