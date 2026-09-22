"""Select concise, stage-aware evidence for local remediation models."""
from __future__ import annotations

import json
from pathlib import Path
import re


LINTIAN_LEVELS = {
    "E": ("error", 0),
    "W": ("warning", 1),
    "I": ("info", 2),
    "P": ("pedantic", 3),
    "X": ("experimental", 4),
    "O": ("override", 5),
}


def _fail_on(command: list[str]) -> list[str]:
    for index, argument in enumerate(command):
        if argument.startswith("--fail-on="):
            return [item.strip() for item in argument.split("=", 1)[1].split(",") if item.strip()]
        if argument == "--fail-on" and index + 1 < len(command):
            return [item.strip() for item in command[index + 1].split(",") if item.strip()]
    return ["error"]


def lintian_evidence(payload: dict, *, supporting_limit: int = 20) -> str:
    """Report tags which meet the recorded Lintian failure policy first."""
    command = payload.get("failed_command") or payload.get("command", {}).get("command", [])
    policy = _fail_on(command)
    configured_ranks = [rank for _, (name, rank) in LINTIAN_LEVELS.items() if name in policy]
    threshold = max(configured_ranks) if configured_ranks else 0
    output = "\n".join(
        filter(None, [payload.get("command", {}).get("stdout", ""), payload.get("command", {}).get("stderr", "")])
    )
    tags = [line for line in output.splitlines() if re.match(r"^[EWIPXO]: ", line)]
    gating = [line for line in tags if LINTIAN_LEVELS[line[0]][1] <= threshold]
    supporting = [line for line in tags if line not in gating][:supporting_limit]
    lines = [
        "STRUCTURED LINTIAN FAILURE",
        f"returncode: {payload.get('command_exit_code')}",
        f"fail_on: {','.join(policy)}",
        f"gating_tags: {len(gating)}",
        f"other_displayed_tags: {len(tags) - len(gating)}",
    ]
    if gating:
        lines.extend(["GATING TAGS:", *gating])
    else:
        lines.extend([
            "GATING TAGS: none were found despite the nonzero return code",
            "DIAGNOSTIC TAIL:",
            *output.splitlines()[-40:],
        ])
    if supporting:
        lines.extend(["LOWER-SEVERITY SAMPLE:", *supporting])
    return "\n".join(lines)


def extract_test_failure_evidence(text: str, *, limit: int = 12_000) -> str:
    """Keep test failures and their tracebacks while dropping package inventories."""
    marker = "displaying pip3 freeze output"
    if marker in text:
        text = text[:text.index(marker)]
    lines = text.splitlines()
    selected: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith(("FAIL: ", "ERROR: ")):
            start = max(0, index - 1)
            end = min(len(lines), index + 35)
            selected.extend(lines[start:end])
        elif re.match(r"^(Ran \d+ tests|FAILED \(|ERROR: InvocationError|ModuleNotFoundError:|AttributeError:)", line):
            selected.append(line)
    # Preserve order while collapsing repeated traceback lines from overlapping windows.
    unique = list(dict.fromkeys(selected))
    body = "\n".join(unique)
    if len(body) > limit:
        body = body[:limit] + "\n[later structured test evidence omitted]"
    return "STRUCTURED TEST FAILURE\n" + body


def structured_failure(path: Path) -> str:
    """Extract the most useful evidence from a LockedBuild failure record."""
    payload = json.loads(path.read_text())
    if payload.get("category") == "LINTIAN":
        return lintian_evidence(payload)
    command = payload.get("command") or {}
    output = "\n".join(filter(None, [command.get("stdout", ""), command.get("stderr", "")]))
    if any(token in output for token in ("FAIL: ", "ERROR: ", "FAILED (")):
        return extract_test_failure_evidence(output)
    return "\n".join([
        "STRUCTURED COMMAND FAILURE",
        f"category: {payload.get('category')}",
        f"returncode: {payload.get('command_exit_code')}",
        output[-8_000:],
    ])
