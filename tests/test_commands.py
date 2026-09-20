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
