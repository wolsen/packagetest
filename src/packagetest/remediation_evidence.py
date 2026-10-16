"""Select concise, stage-aware evidence for local remediation models."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import chain
import hashlib
import json
from pathlib import Path, PurePath
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
            return [
                item.strip()
                for item in argument.split("=", 1)[1].split(",")
                if item.strip()
            ]
        if argument == "--fail-on" and index + 1 < len(command):
            return [
                item.strip() for item in command[index + 1].split(",") if item.strip()
            ]
    return ["error"]


def lintian_evidence(payload: dict, *, supporting_limit: int = 20) -> str:
    """Report tags which meet the recorded Lintian failure policy first."""
    command = payload.get("failed_command") or payload.get("command", {}).get(
        "command", []
    )
    policy = _fail_on(command)
    configured_ranks = [
        rank for _, (name, rank) in LINTIAN_LEVELS.items() if name in policy
    ]
    threshold = max(configured_ranks) if configured_ranks else 0
    output = "\n".join(
        filter(
            None,
            [
                payload.get("command", {}).get("stdout", ""),
                payload.get("command", {}).get("stderr", ""),
            ],
        )
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
        lines.extend(
            [
                "GATING TAGS: none were found despite the nonzero return code",
                "DIAGNOSTIC TAIL:",
                *output.splitlines()[-40:],
            ]
        )
    if supporting:
        lines.extend(["LOWER-SEVERITY SAMPLE:", *supporting])
    return "\n".join(lines)


def extract_test_failure_evidence(text: str, *, limit: int = 12_000) -> str:
    """Keep test failures and their tracebacks while dropping package inventories."""
    marker = "displaying pip3 freeze output"
    if marker in text:
        text = text[: text.index(marker)]
    lines = text.splitlines()
    selected: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith(("FAIL: ", "ERROR: ", "Failed to import test module:")):
            start = max(0, index - 1)
            end = min(len(lines), index + 60)
            selected.extend(lines[start:end])
        elif re.match(
            r"^(Ran \d+ tests|FAILED \(|ERROR: InvocationError|(?:ModuleNotFoundError|ImportError|"
            r"AttributeError|NameError|RuntimeError|ValueError|AssertionError|"
            r"[A-Za-z0-9_.]+Error):)",
            line,
        ):
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
    output = "\n".join(
        filter(None, [command.get("stdout", ""), command.get("stderr", "")])
    )
    if any(token in output for token in ("FAIL: ", "ERROR: ", "FAILED (")):
        return extract_test_failure_evidence(output)
    return "\n".join(
        [
            "STRUCTURED COMMAND FAILURE",
            f"category: {payload.get('category')}",
            f"returncode: {payload.get('command_exit_code')}",
            output[-8_000:],
        ]
    )


def tail(path: Path, limit: int = 10_000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace")
    return text if len(text) <= limit else "[earlier output omitted]\n" + text[-limit:]


def failure_evidence(outputs: Path, tests: Path | None) -> str:
    failure_json = sorted(outputs.rglob("failure.json"))
    blocks = []
    if failure_json:
        blocks.append(structured_failure(failure_json[-1]))
    else:
        build_log = outputs / "nightly-build.log"
        if build_log.is_file():
            blocks.append(tail(build_log, 8_000))
    autopkgtest = tests / "autopkgtest.log" if tests is not None else None
    if autopkgtest is not None and autopkgtest.is_file():
        blocks.append(
            extract_test_failure_evidence(autopkgtest.read_text(errors="replace"))
        )
    result_paths = [outputs / "result.json"]
    if tests is not None:
        result_paths.append(tests / "result/result.json")
    for path in result_paths:
        if path.is_file():
            blocks.append(f"--- {path.name} ---\n{tail(path, 2_000)}")
    return "\n\n".join(blocks)[:20_000]


def failure_focus(evidence: str, limit: int = 3_000) -> str:
    if evidence.startswith("STRUCTURED LINTIAN FAILURE"):
        return evidence[:limit]
    if evidence.startswith("STRUCTURED TEST FAILURE"):
        failure = evidence.rfind("\nFAIL: ")
        if failure >= 0:
            return (evidence.splitlines()[0] + "\n" + evidence[failure:])[-limit:]
        return evidence[-limit:]
    if "ModuleNotFoundError" in evidence:
        relevant = []
        for line in evidence.splitlines():
            if any(
                marker in line
                for marker in (
                    "Failed to import test module:",
                    "ModuleNotFoundError:",
                    "make[",
                    "dpkg-buildpackage: error:",
                )
            ):
                if line not in relevant:
                    relevant.append(line)
        if relevant:
            return "\n".join(relevant)[:limit]
    markers = [
        "ModuleNotFoundError",
        "ImportError",
        "RuntimeError",
        "NameError",
        "ConfigFilesNotFoundError",
        "Hunk #",
        "error: unrecognized arguments",
        "Failures during discovery",
        "subprocess returned exit status",
        "dpkg-buildpackage: error",
    ]
    positions = [evidence.rfind(marker) for marker in markers if marker in evidence]
    if not positions:
        return evidence[-limit:]
    center = min(positions)
    return evidence[max(0, center - 800) : center + limit - 800]


def failure_signature(evidence: str) -> str:
    """Identify materially identical failures without volatile log details."""
    modules = sorted(
        set(
            re.findall(
                r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)", evidence
            )
        )
    )
    if modules:
        value = "missing-modules\n" + "\n".join(modules)
    else:
        value = failure_focus(evidence)
        value = re.sub(r"/home/runner/work/_temp/[^\s'\"]+", "/tmp/WORK", value)
        value = re.sub(r"\b[0-9a-f]{12,64}\b", "HASH", value)
    return hashlib.sha256(value.encode()).hexdigest()


def _patch_names(evidence: str) -> set[str]:
    names = set(re.findall(r"([A-Za-z0-9][A-Za-z0-9_.+~-]*\.patch)", evidence))
    failed = set(
        re.findall(
            r"(?:patch\s+['\"]|applying\s+)([A-Za-z0-9][A-Za-z0-9_.+~-]*\.patch)",
            evidence,
            re.I,
        )
    )
    explicit = set(re.findall(r"if patch ['\"]([^'\"]+\.patch)['\"]", evidence, re.I))
    return explicit or (names & failed if failed else names)


def _source_build_dependencies(text: str) -> str:
    """Preserve folded build fields without including binary package stanzas."""
    paragraph = re.split(r"\n\s*\n", text, maxsplit=1)[0]
    lines = paragraph.splitlines(keepends=True)
    fields = []
    index = 0
    while index < len(lines):
        if not re.match(r"^(Build-Depends(?:-Indep)?):", lines[index]):
            index += 1
            continue
        block = [lines[index]]
        index += 1
        while index < len(lines) and lines[index].startswith((" ", "\t")):
            block.append(lines[index])
            index += 1
        fields.append("".join(block).rstrip())
    return "\n".join(fields)


def _missing_import_context(tree: Path, evidence: str) -> str:
    control = tree / "debian/control"
    if not control.is_file() or control.is_symlink():
        return ""
    fields = _source_build_dependencies(control.read_text(errors="replace"))
    modules = list(
        dict.fromkeys(
            re.findall(
                r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)", evidence
            )
        )
    )
    candidates = [
        "python3-" + module.split(".")[0].replace("_", "-").lower()
        for module in modules
    ]
    value = "\n--- debian/control source build dependencies ---\n" + fields + "\n"
    if candidates:
        value += "\n--- Debian package candidates inferred from missing imports ---\n"
        value += "\n".join(dict.fromkeys(candidates)) + "\n"
    return value


def _context_paths(evidence: str, patches: set[str]) -> list[str]:
    if patches:
        # The series and failed patch take priority over large control files.
        return [
            "debian/patches/series",
            *(f"debian/patches/{name}" for name in sorted(patches)),
        ]
    if not re.search(r"(?m)^FAIL: ", evidence):
        return ["debian/rules", "debian/control", "debian/patches/series"]
    relative = []
    for value in re.findall(r'File "(?:/<<PKGBUILDDIR>>/)?([^"\n]+\.py)"', evidence):
        path = PurePath(value)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] == ("debian",):
            continue
        if path.as_posix() not in relative:
            relative.append(path.as_posix())
    return relative[:6]


def _readable_context_file(path: Path) -> bool:
    return (
        path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024
    )


def _file_blocks(tree: Path, relative: list[str]) -> Iterator[str]:
    for name in relative:
        path = tree / name
        if _readable_context_file(path):
            yield f"\n--- {name} ---\n{path.read_text(errors='replace')}\n"


def _hunk_centers(patch: str, lines: list[str]) -> set[int]:
    centers = {
        max(0, int(line) - 1)
        for line in re.findall(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)", patch, re.M)
    }
    changed = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    tokens = set(re.findall(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", changed))
    # Definitions locate relevant code when upstream hunk line numbers drift.
    for index, line in enumerate(lines):
        if any(token in line for token in tokens):
            centers.add(index)
    return centers


def _focused_source(patch: str, lines: list[str]) -> str:
    ranges: list[tuple[int, int]] = []
    for center in sorted(_hunk_centers(patch, lines))[:8]:
        start, end = max(0, center - 18), min(len(lines), center + 19)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    excerpts = [
        "\n".join(f"{index + 1:>6}: {lines[index]}" for index in range(start, end))
        for start, end in ranges[:4]
    ]
    return "\n...\n".join(excerpts) if excerpts else "\n".join(lines[:80])


def _patch_blocks(tree: Path, patches: set[str]) -> Iterator[str]:
    for name in sorted(patches):
        patch = tree / "debian/patches" / name
        if not patch.is_file():
            continue
        text = patch.read_text(errors="replace")
        targets = re.findall(r"^\+\+\+ (?:b/)?([^\t\n ]+)", text, re.M)
        for target in targets[:4]:
            path = tree / target
            if _readable_context_file(path):
                body = _focused_source(
                    text, path.read_text(errors="replace").splitlines()
                )
                yield f"\n--- focused current upstream {target} ---\n{body}\n"


def _missing_path_blocks(tree: Path, evidence: str) -> Iterator[str]:
    rules_path = tree / "debian/rules"
    rules = rules_path.read_text(errors="replace") if rules_path.is_file() else ""
    mentioned = {
        value
        for value in re.findall(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", evidence
        )
        if value in rules
    }
    for old in sorted(mentioned):
        if (tree / old).exists():
            continue
        matches = [
            path
            for path in tree.rglob(PurePath(old).name)
            if "debian" not in path.relative_to(tree).parts and path.is_file()
        ][:8]
        if matches:
            listing = "\n".join(str(path.relative_to(tree)) for path in matches)
            yield f"\n--- upstream candidates for missing {old} ---\n{listing}\n"


def source_context(tree: Path, evidence: str, limit: int = 12_000) -> str:
    """Select evidence in priority order, sharing one character budget."""
    if "ModuleNotFoundError" in evidence:
        return _missing_import_context(tree, evidence)[:limit]
    patches = _patch_names(evidence)
    blocks = chain(
        _file_blocks(tree, _context_paths(evidence, patches)),
        _patch_blocks(tree, patches),
        _missing_path_blocks(tree, evidence),
    )
    selected = []
    remaining = limit
    for block in blocks:
        selected.append(block[:remaining])
        remaining -= len(selected[-1])
        if remaining <= 0:
            break
    return "".join(selected)
