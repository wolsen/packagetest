from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .models import CommandResult


class CommandRunner:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def run(self, command: list[str], cwd: Path, env_diff: dict[str, str] | None = None) -> CommandResult:
        env_diff = env_diff or {}
        env = os.environ.copy()
        env.update(env_diff)
        start = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )
        duration = time.monotonic() - start
        result = CommandResult(
            command=command,
            cwd=str(cwd),
            env_diff=env_diff,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            duration_seconds=duration,
        )
        self._append(result)
        return result

    def _append(self, result: CommandResult) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict(), sort_keys=True))
            f.write("\n")
