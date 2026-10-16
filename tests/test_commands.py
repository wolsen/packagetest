import json
from pathlib import Path

from packagetest.commands import CommandRunner


def test_command_runner_logs_command_result(tmp_path: Path):
    log_path = tmp_path / "commands.jsonl"
    runner = CommandRunner(log_path=log_path)

    result = runner.run(
        command=["bash", "-lc", "echo hello && echo oops 1>&2"],
        cwd=tmp_path,
        env_diff={"EXAMPLE_FLAG": "1"},
    )

    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert "oops" in result.stderr

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["cwd"] == str(tmp_path)
    assert payload["env_diff"]["EXAMPLE_FLAG"] == "1"
    assert payload["exit_code"] == 0


def test_command_runner_emits_debug_logs(tmp_path: Path, caplog):
    log_path = tmp_path / "commands.jsonl"
    runner = CommandRunner(log_path=log_path)

    with caplog.at_level("DEBUG"):
        runner.run(
            command=["bash", "-lc", "echo hello && echo oops 1>&2"],
            cwd=tmp_path,
            env_diff={},
        )

    assert any("Running command:" in message for message in caplog.messages)
    assert any("Command stdout" in message and "hello" in message for message in caplog.messages)
    assert any("Command stderr" in message and "oops" in message for message in caplog.messages)


def test_timeout_kills_command(tmp_path):
    runner = CommandRunner(tmp_path / 'commands.jsonl', timeout=0.2)
    result = runner.run(['sleep', '10'], tmp_path)
    assert result.exit_code == 124
    assert result.duration_seconds < 3


def test_missing_executable_is_logged(tmp_path):
    runner = CommandRunner(tmp_path / 'commands.jsonl')
    assert runner.run(['/nonexistent/packaging-tool'], tmp_path).exit_code == 127
    assert (tmp_path / '0001.stderr.log').read_text()


def test_timeout_remains_active_after_output_streams_close(tmp_path):
    import sys

    runner = CommandRunner(tmp_path / 'commands.jsonl', timeout=0.1)
    result = runner.run([sys.executable, '-c',
                         'import os,time; os.close(1); os.close(2); time.sleep(5)'], tmp_path)
    assert result.exit_code == 124
    assert result.duration_seconds < 3


def test_complete_stdout_is_available_beyond_diagnostic_tail(tmp_path: Path) -> None:
    """Large command output remains readable without exposing runner bookkeeping."""
    import sys

    result = CommandRunner(tmp_path / 'commands.jsonl').run(
        [sys.executable, '-c', "print('BEGIN' + 'x' * 300000 + 'END')"], tmp_path)

    assert len(result.stdout_tail.encode()) <= 262144
    assert not result.stdout_tail.startswith('BEGIN')
    assert result.read_stdout().startswith('BEGIN')
    assert result.read_stdout().endswith('END\n')
    assert Path(result.stderr_path).is_file()
    assert result.to_dict()['stdout_path'] == result.stdout_path


def test_preparation_and_discovery_accept_executor_without_log_bookkeeping(tmp_path: Path) -> None:
    """Injected executors provide complete output through results, with arbitrary paths."""
    from packagetest.models import CommandResult
    from packagetest.nightly_source import SourcePreparer
    from packagetest.snapshot_lock import Resolver

    output = tmp_path / 'custom-output.txt'
    output.write_text('complete output\n')

    class Executor:
        """Supply command results without a sequence counter or log directory."""

        def run(self, command, cwd, env_diff=None):
            """Return a short diagnostic tail and an explicitly located full capture."""
            return CommandResult(command, str(cwd), env_diff or {}, 'tail', '', 0, 0.0,
                                 stdout_path=str(output))

    preparer = SourcePreparer(tmp_path / 'source', runner=Executor())
    resolver = Resolver(tmp_path / 'discovery', runner=Executor())
    assert preparer.command('example') == 'complete output'
    assert resolver.command('example') == 'complete output'
