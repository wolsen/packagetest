"""Print bounded diagnostics from packaging failure bundles and command traces."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _tail_lines(text: str, limit: int = 30) -> list[str]:
    """Retain the final diagnostic lines within the requested display budget."""
    return text.splitlines()[-limit:]


def _safe_string_list(value: object) -> list[str]:
    """Accept a complete string list or discard malformed diagnostic command data."""
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return value
    return []


def _load_command_results(commands_path: Path) -> tuple[list[dict[str, object]], int]:
    """Read JSONL command evidence while counting and reporting unreadable records."""
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
    """Match a source name against complete working-directory path segments."""
    cwd = command_result.get("cwd")
    return isinstance(cwd, str) and source in Path(cwd).parts


def _print_trace_result(index: int, result: dict[str, object]) -> None:
    """Print command identity and both nonempty diagnostic stream tails."""
    command_text = " ".join(_safe_string_list(result.get("command")))
    exit_code = result.get("exit_code")
    duration = result.get("duration_seconds")
    cwd = result.get("cwd")
    print(f"    [{index}] exit_code={exit_code} duration_seconds={duration} cwd={cwd}")
    print(f"      command: {command_text}")
    _print_trace_stream("stdout", result.get("stdout"))
    _print_trace_stream("stderr", result.get("stderr"))


def _print_trace_stream(label: str, value: object) -> None:
    """Show at most ten lines from a nonempty string-valued command stream."""
    if isinstance(value, str) and value.strip():
        print(f"      {label}_tail:")
        for line in _tail_lines(value.strip(), limit=10):
            print(f"        {line}")


def _commands_for_source(results: list[dict[str, object]], source: str) -> list[dict[str, object]]:
    """Select commands whose working directory contains the exact source path segment."""
    return [result for result in results if _matches_source(result, source)]


def _print_command_trace(*, root: Path, generation_id: str | None, source: str) -> int:
    """Print available per-source commands without treating missing traces as errors."""
    if not generation_id:
        print("  command_trace: generation id missing")
        return 0

    commands_path = root / generation_id / "logs" / "commands.jsonl"
    if not commands_path.exists():
        print(f"  command_trace: log not found at {commands_path}")
        return 0

    all_results, parse_errors = _load_command_results(commands_path)
    source_results = _commands_for_source(all_results, source)
    if not source_results:
        print(f"  command_trace: no commands found for source {source} in {commands_path}")
        return parse_errors

    print(f"  command_trace: {len(source_results)} command(s) from {commands_path}")
    for index, result in enumerate(source_results, start=1):
        _print_trace_result(index, result)

    return parse_errors


def _valid_argv(value: object) -> bool:
    """Recognize a JSON argument vector made exclusively of strings."""
    return isinstance(value, list) and all(isinstance(part, str) for part in value)


def _failure_argv(command: dict, payload: dict) -> list:
    """Prefer structured argv, falling back to the legacy failed-command field."""
    value = command.get("command")
    if not _valid_argv(value):
        value = payload.get("failed_command")

    return value if isinstance(value, list) else []


def _print_failure_stream(command: dict) -> None:
    """Prefer the failure's stderr tail, using stdout only when stderr is empty."""
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


def _summarize_failure(root: Path, failure_file: Path, payload: dict) -> int:
    """Print the direct failure and return unreadable command-log record counts."""
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    command_argv = _failure_argv(command, payload)

    print(f"- package: {payload.get('source_package')}")
    print(f"  category: {payload.get('category')}")
    print(f"  error: {payload.get('error', 'unspecified')}")
    print(f"  failed_command: {' '.join(command_argv)}")
    print(f"  exit_code: {payload.get('command_exit_code')}")
    print(f"  failure_json: {failure_file}")
    _print_failure_stream(command)
    source_package = payload.get("source_package")
    generation_id = payload.get("generation_id")
    if isinstance(source_package, str):
        return _print_command_trace(root=root, generation_id=generation_id if isinstance(generation_id, str) else None, source=source_package)

    return 0


def summarize(root: Path) -> int:
    """Summarize all failure bundles and signal malformed diagnostic records."""
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

        parse_errors += _summarize_failure(root, failure_file, payload)

    if parse_errors:
        print(f"Encountered {parse_errors} unreadable failure bundle(s).", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    """Summarize packaging failures beneath the requested artifact root."""
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts")
    return summarize(base)


if __name__ == "__main__":
    raise SystemExit(main())
