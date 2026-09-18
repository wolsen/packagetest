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


def test_summarize_outputs_stderr_tail_and_succeeds(tmp_path: Path, capsys):
    module = _load_module()
    failure_file = tmp_path / "gen-1" / "failures" / "pbr" / "failure.json"
    _write_failure(
        failure_file,
        {
            "source_package": "pbr",
            "category": "COMPILATION_FAILURE",
            "command_exit_code": 2,
            "command": {
                "command": ["sbuild", "--dist=noble"],
                "stdout": "stdout line",
                "stderr": "stderr one\nstderr two",
            },
        },
    )

    assert module.summarize(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Found 1 packaging failure bundle(s)." in out
    assert "failed_command: sbuild --dist=noble" in out
    assert "stderr_tail:" in out
    assert "stderr one" in out
    assert "stdout_tail:" not in out


def test_summarize_returns_nonzero_for_invalid_json(tmp_path: Path, capsys):
    module = _load_module()
    failure_file = tmp_path / "gen-1" / "failures" / "pbr" / "failure.json"
    failure_file.parent.mkdir(parents=True, exist_ok=True)
    failure_file.write_text("{", encoding="utf-8")

    assert module.summarize(tmp_path) == 1
    captured = capsys.readouterr()
    assert "parse_error" in captured.out
    assert "unreadable failure bundle" in captured.err

