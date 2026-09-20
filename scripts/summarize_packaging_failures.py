from __future__ import annotations

import json
import sys
from pathlib import Path


def _tail_lines(text: str, limit: int = 30) -> list[str]:
    return text.splitlines()[-limit:]


def _safe_string_list(value: object) -> list[str]:
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return value
    return []


def _load_command_results(commands_path: Path) -> tuple[list[dict[str, object]], int]:
    try:
        lines = commands_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"  command_log_error: {commands_path}: {exc}")
        return [], 1

    results: list[dict[str, object]] = []
    parse_errors = 0
    for index, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors += 1
            print(f"  command_log_parse_error: {commands_path}:{index}: {exc}")
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results, parse_errors


def _matches_source(command_result: dict[str, object], source: str) -> bool:
    cwd = command_result.get("cwd")
    return isinstance(cwd, str) and source in Path(cwd).parts


def _print_command_trace(*, root: Path, generation_id: str | None, source: str) -> int:
    if not generation_id:
        print("  command_trace: generation id missing")
        return 0
    commands_path = root / generation_id / "logs" / "commands.jsonl"
    if not commands_path.exists():
        print(f"  command_trace: log not found at {commands_path}")
        return 0

    all_results, parse_errors = _load_command_results(commands_path)
    source_results = [result for result in all_results if _matches_source(result, source)]
    if not source_results:
        print(f"  command_trace: no commands found for source {source} in {commands_path}")
        return parse_errors

    print(f"  command_trace: {len(source_results)} command(s) from {commands_path}")
    for index, result in enumerate(source_results, start=1):
        command_text = " ".join(_safe_string_list(result.get("command")))
        exit_code = result.get("exit_code")
        duration = result.get("duration_seconds")
        cwd = result.get("cwd")
        print(f"    [{index}] exit_code={exit_code} duration_seconds={duration} cwd={cwd}")
        print(f"      command: {command_text}")
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if isinstance(stdout, str) and stdout.strip():
            print("      stdout_tail:")
            for line in _tail_lines(stdout.strip(), limit=10):
                print(f"        {line}")
        if isinstance(stderr, str) and stderr.strip():
            print("      stderr_tail:")
            for line in _tail_lines(stderr.strip(), limit=10):
                print(f"        {line}")
    return parse_errors


def summarize(root: Path) -> int:
    failure_files = sorted(root.glob("gen-*/failures/*/failure.json"))
    if not failure_files:
        print("No packaging failure bundles found.")
        return 0

    parse_errors = 0
    print(f"Found {len(failure_files)} packaging failure bundle(s).")
    for failure_file in failure_files:
        try:
            payload = json.loads(failure_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            parse_errors += 1
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
        print(f"  error: {payload.get('error', 'unspecified')}")
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
        source_package = payload.get("source_package")
        generation_id = payload.get("generation_id")
        if isinstance(source_package, str):
            parse_errors += _print_command_trace(root=root, generation_id=generation_id if isinstance(generation_id, str) else None, source=source_package)
    if parse_errors:
        print(f"Encountered {parse_errors} unreadable failure bundle(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts")
    raise SystemExit(summarize(base))
