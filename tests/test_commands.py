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
