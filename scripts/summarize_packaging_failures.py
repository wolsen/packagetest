from __future__ import annotations

import json
import sys
from pathlib import Path


def _tail_lines(text: str, limit: int = 30) -> list[str]:
    return text.splitlines()[-limit:]


def summarize(root: Path) -> int:
    failure_files = sorted(root.glob("gen-*/failures/*/failure.json"))
    if not failure_files:
        print("No packaging failure bundles found.")
        return 0

    print(f"Found {len(failure_files)} packaging failure bundle(s).")
    for failure_file in failure_files:
        try:
            payload = json.loads(failure_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"- failure_json: {failure_file}")
            print(f"  parse_error: {exc}")
            continue

        command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        command_argv = command.get("command")
        if not isinstance(command_argv, list) or not all(isinstance(part, str) for part in command_argv):
            command_argv = payload.get("failed_command")
        if not isinstance(command_argv, list):
            command_argv = []

        print(f"- package: {payload.get('source_package')}")
        print(f"  category: {payload.get('category')}")
        print(f"  failed_command: {' '.join(command_argv)}")
        print(f"  exit_code: {payload.get('command_exit_code')}")
        print(f"  failure_json: {failure_file}")
        stderr = str(command.get("stderr", "")).strip()
        stdout = str(command.get("stdout", "")).strip()
        if stderr:
            print("  stderr_tail:")
            for line in _tail_lines(stderr):
                print(f"    {line}")
        elif stdout:
            print("  stdout_tail:")
            for line in _tail_lines(stdout):
                print(f"    {line}")
    return 0


if __name__ == "__main__":
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts")
    raise SystemExit(summarize(base))
