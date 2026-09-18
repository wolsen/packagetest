import json
from argparse import Namespace
from pathlib import Path

from packagetest.cli import _run_package, plan_cmd
from packagetest.commands import CommandRunner
from packagetest.config import load_package_definitions
from packagetest.models import BuildState, CommandResult, PackageExecutionMetadata
from packagetest.packaging import build_package_operation_plan
from packagetest.planner import build_plan
from packagetest.release_discovery import ResolvedRelease


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


def test_run_package_dry_run_marks_published(tmp_path: Path):
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

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan)

    assert states["pbr"] == BuildState.PUBLISHED
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

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan)

    assert states["pbr"] == BuildState.BUILD_SUCCEEDED
    assert metadata.build_finished_at != "unknown"


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

    _run_package("pbr", plan, args, tmp_path, runner, states, metadata, operation_plan)

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
