from argparse import Namespace
from pathlib import Path

from packagetest.cli import _run_package
from packagetest.commands import CommandRunner
from packagetest.config import load_package_definitions
from packagetest.models import BuildState, CommandResult
from packagetest.planner import build_plan


class FailingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command, cwd, env_diff=None):
        self.calls += 1
        return CommandResult(
            command=command,
            cwd=str(cwd),
            env_diff=env_diff or {},
            stdout="",
            stderr="failed",
            exit_code=1,
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

    _run_package("pbr", plan, args, tmp_path, runner, states)

    assert states["pbr"] == BuildState.PUBLISHED
    assert (tmp_path / "commands.jsonl").exists()


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
    runner = FailingRunner()

    _run_package("pbr", plan, args, tmp_path, runner, states)

    assert states["pbr"] == BuildState.BUILD_FAILED
    assert (tmp_path / "failures" / "pbr" / "failure.json").exists()
    assert runner.calls == 1
