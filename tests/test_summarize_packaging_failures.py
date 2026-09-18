from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "summarize_packaging_failures.py"
    spec = importlib.util.spec_from_file_location("summarize_packaging_failures", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_failure(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_command_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_summarize_outputs_stderr_tail_and_succeeds(tmp_path: Path, capsys):
    module = _load_module()
    generation_id = "gen-1"
    failure_file = tmp_path / generation_id / "failures" / "pbr" / "failure.json"
    _write_failure(
        failure_file,
        {
            "source_package": "pbr",
            "generation_id": generation_id,
            "category": "COMPILATION_FAILURE",
            "command_exit_code": 2,
            "command": {
                "command": ["sbuild", "--dist=noble"],
                "stdout": "stdout line",
                "stderr": "stderr one\nstderr two",
            },
        },
    )
    _write_command_results(
        tmp_path / generation_id / "logs" / "commands.jsonl",
        [
            {
                "command": ["git", "clone", "repo"],
                "cwd": f"/tmp/{generation_id}/pbr",
                "stdout": "clone ok",
                "stderr": "",
                "exit_code": 0,
                "duration_seconds": 0.2,
            },
            {
                "command": ["sbuild", "--dist=noble"],
                "cwd": f"/tmp/{generation_id}/pbr",
                "stdout": "",
                "stderr": "build failed line 1\nbuild failed line 2",
                "exit_code": 2,
                "duration_seconds": 12.4,
            },
        ],
    )

    assert module.summarize(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Found 1 packaging failure bundle(s)." in out
    assert "failed_command: sbuild --dist=noble" in out
    assert "stderr_tail:" in out
    assert "stderr one" in out
    assert "command_trace: 2 command(s)" in out
    assert "command: git clone repo" in out
    assert "command: sbuild --dist=noble" in out


def test_summarize_returns_nonzero_for_invalid_json(tmp_path: Path, capsys):
    module = _load_module()
    failure_file = tmp_path / "gen-1" / "failures" / "pbr" / "failure.json"
    failure_file.parent.mkdir(parents=True, exist_ok=True)
    failure_file.write_text("{", encoding="utf-8")

    assert module.summarize(tmp_path) == 1
    captured = capsys.readouterr()
    assert "parse_error" in captured.out
    assert "unreadable failure bundle" in captured.err


def test_summarize_returns_nonzero_for_invalid_command_log_json(tmp_path: Path, capsys):
    module = _load_module()
    generation_id = "gen-1"
    failure_file = tmp_path / generation_id / "failures" / "pbr" / "failure.json"
    _write_failure(
        failure_file,
        {
            "source_package": "pbr",
            "generation_id": generation_id,
            "category": "COMPILATION_FAILURE",
            "command_exit_code": 2,
            "command": {
                "command": ["sbuild", "--dist=noble"],
                "stdout": "",
                "stderr": "boom",
            },
        },
    )
    commands_file = tmp_path / generation_id / "logs" / "commands.jsonl"
    commands_file.parent.mkdir(parents=True, exist_ok=True)
    commands_file.write_text("{\n", encoding="utf-8")

    assert module.summarize(tmp_path) == 1
    captured = capsys.readouterr()
    assert "command_log_parse_error" in captured.out
    assert "unreadable failure bundle" in captured.err
