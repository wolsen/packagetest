"""Select concise, stage-aware evidence for local remediation models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from itertools import chain
from pathlib import Path, PurePath

from .contracts import object_mapping, string_list, text_field

LINTIAN_LEVELS = {
    "E": ("error", 0),
    "W": ("warning", 1),
    "I": ("info", 2),
    "P": ("pedantic", 3),
    "X": ("experimental", 4),
    "O": ("override", 5),
}


def _policy_values(value: str) -> list[str]:
    """Parse comma-separated Lintian severities, dropping empty values."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _fail_on(command: list[str]) -> list[str]:
    """Read the first complete failure-policy option, defaulting to errors only."""
    for index, argument in enumerate(command):
        if argument.startswith("--fail-on="):
            return _policy_values(argument.split("=", 1)[1])

        if argument == "--fail-on" and index + 1 < len(command):
            return _policy_values(command[index + 1])

    return ["error"]


def _lintian_threshold(policy: list[str]) -> int:
    """Use the least severe configured level as the inclusive failure threshold."""
    ranks = [rank for name, rank in LINTIAN_LEVELS.values() if name in policy]
    return max(ranks) if ranks else 0


def _lintian_tags(output: str, threshold: int) -> tuple[list[str], list[str]]:
    """Separate recognized tags from those meeting the recorded failure threshold."""
    tags = [line for line in output.splitlines() if re.match(r"^[EWIPXO]: ", line)]
    gating = [line for line in tags if LINTIAN_LEVELS[line[0]][1] <= threshold]
    return tags, gating


def _lintian_details(
    output: str, gating: list[str], supporting: list[str]
) -> list[str]:
    """Show gating tags, or a diagnostic tail when the exit status lacks matching tags."""
    if gating:
        lines = ["GATING TAGS:", *gating]
    else:
        lines = [
            "GATING TAGS: none were found despite the nonzero return code",
            "DIAGNOSTIC TAIL:",
            *output.splitlines()[-40:],
        ]

    if supporting:
        lines.extend(["LOWER-SEVERITY SAMPLE:", *supporting])

    return lines


def lintian_evidence(
    payload: Mapping[str, object], *, supporting_limit: int = 20
) -> str:
    """Report tags which meet the recorded Lintian failure policy first."""
    capture = object_mapping(payload.get("command") or {}, "command evidence")
    command = string_list(
        payload.get("failed_command") or capture.get("command", []), "failed_command"
    )
    policy = _fail_on(command)
    threshold = _lintian_threshold(policy)
    output = "\n".join(
        filter(
            None,
            [
                text_field(capture, "stdout"),
                text_field(capture, "stderr"),
            ],
        )
    )
    tags, gating = _lintian_tags(output, threshold)
    supporting = [line for line in tags if line not in gating][:supporting_limit]
    lines = [
        "STRUCTURED LINTIAN FAILURE",
        f"returncode: {payload.get('command_exit_code')}",
        f"fail_on: {','.join(policy)}",
        f"gating_tags: {len(gating)}",
        f"other_displayed_tags: {len(tags) - len(gating)}",
    ]
    lines.extend(_lintian_details(output, gating, supporting))
    return "\n".join(lines)


def _test_failure_lines(lines: list[str]) -> list[str]:
    """Select failure windows and standalone exception summaries in log order."""
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

    return selected


def extract_test_failure_evidence(text: str, *, limit: int = 12_000) -> str:
    """Keep test failures and their tracebacks while dropping package inventories."""
    marker = "displaying pip3 freeze output"
    if marker in text:
        text = text[: text.index(marker)]

    lines = text.splitlines()
    selected = _test_failure_lines(lines)
    # Preserve order while collapsing repeated traceback lines from overlapping windows.
    unique = list(dict.fromkeys(selected))
    body = "\n".join(unique)
    if len(body) > limit:
        body = body[:limit] + "\n[later structured test evidence omitted]"

    return "STRUCTURED TEST FAILURE\n" + body


def structured_failure(path: Path) -> str:
    """Extract the most useful evidence from a LockedBuild failure record."""
    payload = object_mapping(json.loads(path.read_text()), "failure evidence")
    if payload.get("category") == "LINTIAN":
        return lintian_evidence(payload)

    command = object_mapping(payload.get("command") or {}, "command evidence")
    output = "\n".join(
        filter(None, [text_field(command, "stdout"), text_field(command, "stderr")])
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
    """Read a bounded file tail, marking omitted earlier content."""
    if not path.is_file():
        return ""

    text = path.read_text(errors="replace")
    return text if len(text) <= limit else "[earlier output omitted]\n" + text[-limit:]


def _build_failure_blocks(outputs: Path) -> list[str]:
    """Prefer the latest structured build failure over the general build log."""
    failures = sorted(outputs.rglob("failure.json"))
    if failures:
        return [structured_failure(failures[-1])]

    log = outputs / "nightly-build.log"
    return [tail(log, 8_000)] if log.is_file() else []


def _test_failure_blocks(tests: Path | None) -> list[str]:
    """Read structured test evidence only when an autopkgtest log is available."""
    if tests is None:
        return []

    log = tests / "autopkgtest.log"
    if not log.is_file():
        return []

    return [extract_test_failure_evidence(log.read_text(errors="replace"))]


def failure_evidence(outputs: Path, tests: Path | None) -> str:
    """Combine build, test, and result evidence in priority order within one budget."""
    blocks = _build_failure_blocks(outputs) + _test_failure_blocks(tests)
    paths = [outputs / "result.json"]
    if tests is not None:
        paths.append(tests / "result/result.json")

    for path in paths:
        if path.is_file():
            blocks.append(f"--- {path.name} ---\n{tail(path, 2_000)}")

    return "\n\n".join(blocks)[:20_000]


def _structured_test_focus(evidence: str, limit: int) -> str:
    """Prefer the final named test failure while retaining the structured heading."""
    failure = evidence.rfind("\nFAIL: ")
    if failure >= 0:
        return (evidence.splitlines()[0] + "\n" + evidence[failure:])[-limit:]

    return evidence[-limit:]


def _missing_module_focus(evidence: str, limit: int) -> str | None:
    """Deduplicate import-failure and enclosing build-error lines without reordering them."""
    markers = (
        "Failed to import test module:",
        "ModuleNotFoundError:",
        "make[",
        "dpkg-buildpackage: error:",
    )
    relevant = [
        line
        for line in evidence.splitlines()
        if any(marker in line for marker in markers)
    ]
    return "\n".join(dict.fromkeys(relevant))[:limit] if relevant else None


def _diagnostic_focus(evidence: str, limit: int) -> str:
    """Center context around the earliest last-occurring diagnostic marker."""
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


def failure_focus(evidence: str, limit: int = 3_000) -> str:
    """Choose stage-specific prompt evidence before falling back to diagnostic markers."""
    if evidence.startswith("STRUCTURED LINTIAN FAILURE"):
        return evidence[:limit]

    if evidence.startswith("STRUCTURED TEST FAILURE"):
        return _structured_test_focus(evidence, limit)

    if "ModuleNotFoundError" in evidence:
        focus = _missing_module_focus(evidence, limit)
        if focus is not None:
            return focus

    return _diagnostic_focus(evidence, limit)


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
    """Prefer explicitly diagnosed patch names over general mentions."""
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
    """Show source build dependencies and inferred Debian packages for missing imports."""
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
    """Prioritize failed quilt patches before traceback or general packaging context."""
    if patches:
        # The series and failed patch take priority over large control files.
        return [
            "debian/patches/series",
            *(f"debian/patches/{name}" for name in sorted(patches)),
        ]

    if not re.search(r"(?m)^FAIL: ", evidence):
        return ["debian/rules", "debian/control", "debian/patches/series"]

    return _traceback_paths(evidence)[:6]


def _allowed_traceback_path(path: PurePath) -> bool:
    """Exclude absolute, traversing, and Debian packaging paths from traceback context."""
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and path.parts[:1] != ("debian",)
    )


def _traceback_paths(evidence: str) -> list[str]:
    """Collect unique safe Python traceback paths in their first-seen order."""
    relative = []
    for value in re.findall(r'File "(?:/<<PKGBUILDDIR>>/)?([^"\n]+\.py)"', evidence):
        path = PurePath(value)
        if _allowed_traceback_path(path):
            relative.append(path.as_posix())

    return list(dict.fromkeys(relative))


def _readable_context_file(path: Path) -> bool:
    """Require a small regular nonsymlink file for model source context."""
    return (
        path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024
    )


def _file_blocks(tree: Path, relative: list[str]) -> Iterator[str]:
    """Render eligible source files in the caller-specified priority order."""
    for name in relative:
        path = tree / name
        if _readable_context_file(path):
            yield f"\n--- {name} ---\n{path.read_text(errors='replace')}\n"


def _changed_definition_tokens(patch: str) -> set[str]:
    """Extract definition names from changed payload lines rather than diff headers."""
    changed = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    return set(re.findall(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", changed))


def _definition_centers(lines: list[str], tokens: set[str]) -> set[int]:
    """Find current definition locations when upstream hunk line numbers have drifted."""
    return {
        index
        for index, line in enumerate(lines)
        if any(token in line for token in tokens)
    }


def _hunk_centers(patch: str, lines: list[str]) -> set[int]:
    """Combine declared hunk positions with matching current definition locations."""
    centers = {
        max(0, int(line) - 1)
        for line in re.findall(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)", patch, re.M)
    }
    return centers | _definition_centers(lines, _changed_definition_tokens(patch))


def _excerpt_ranges(patch: str, lines: list[str]) -> list[tuple[int, int]]:
    """Merge overlapping windows around the first eight relevant hunk centers."""
    ranges: list[tuple[int, int]] = []
    for center in sorted(_hunk_centers(patch, lines))[:8]:
        start, end = max(0, center - 18), min(len(lines), center + 19)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    return ranges[:4]


def _focused_source(patch: str, lines: list[str]) -> str:
    """Render numbered focused excerpts, falling back to the first eighty source lines."""
    excerpts = [
        "\n".join(f"{index + 1:>6}: {lines[index]}" for index in range(start, end))
        for start, end in _excerpt_ranges(patch, lines)
    ]
    return "\n...\n".join(excerpts) if excerpts else "\n".join(lines[:80])


def _patch_blocks(tree: Path, patches: set[str]) -> Iterator[str]:
    """Render focused upstream files named by the first four targets in each patch."""
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


def _mentioned_packaging_paths(tree: Path, evidence: str) -> set[str]:
    """Find evidence paths that are actually referenced by Debian rules."""
    rules_path = tree / "debian/rules"
    rules = rules_path.read_text(errors="replace") if rules_path.is_file() else ""
    mentioned = {
        value
        for value in re.findall(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", evidence
        )
        if value in rules
    }
    return mentioned


def _upstream_path_candidates(tree: Path, old: str) -> list[Path]:
    """Find at most eight source files sharing a missing path's basename outside Debian metadata."""
    return [
        path
        for path in tree.rglob(PurePath(old).name)
        if "debian" not in path.relative_to(tree).parts and path.is_file()
    ][:8]


def _missing_path_blocks(tree: Path, evidence: str) -> Iterator[str]:
    """Suggest upstream basename matches only for referenced packaging paths that are missing."""
    for old in sorted(_mentioned_packaging_paths(tree, evidence)):
        if (tree / old).exists():
            continue

        matches = _upstream_path_candidates(tree, old)
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
