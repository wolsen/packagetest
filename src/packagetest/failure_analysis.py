"""Bounded, advisory analysis of packaging failures by a local model."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Iterable


BUILD_FAILURES = {"FAILED", "INFRA_ERROR"}
TEST_FAILURES = {"FAIL", "INFRA_ERROR"}
ALLOWED_PATCH_ROOTS = {"config", "scripts", "src", "tests"}
FORBIDDEN_PATCH_PREFIXES = (".github/", ".git/", "artifacts/")
TEXT_SUFFIXES = {
    "", ".build", ".buildinfo", ".changes", ".conf", ".control", ".dsc",
    ".ini", ".json", ".log", ".md", ".patch", ".py", ".rules", ".sh",
    ".txt", ".yaml", ".yml",
}


def select_direct_failures(rows: Iterable[dict]) -> list[dict]:
    """Select one actionable phase per source and omit dependency blocking."""
    selected = []
    for row in rows:
        source = row["source"]
        build = row.get("build", {}).get("result")
        test = row.get("autopkgtest", {}).get("result")
        if build in BUILD_FAILURES:
            selected.append({"source": source, "phase": "build", "result": build})
        elif test in TEST_FAILURES:
            selected.append({"source": source, "phase": "autopkgtest", "result": test})

    return selected


@dataclass(frozen=True)
class ModelResponse:
    """A model diagnosis and its extracted advisory patch."""

    diagnosis: str
    patch: str


REPAIR_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "add_dependency", "remove_rule_argument", "refresh_quilt_patch", "drop_quilt_patch",
            "replace_packaging_path", "no_fix",
        ]},
        "subject": {"type": "string"},
        "replacement": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["action", "subject", "replacement", "evidence"],
    "additionalProperties": False,
}


def _decoded_decision(text: str, decoder: json.JSONDecoder) -> dict | None:
    """Decode one candidate object only when all required decision fields are present."""
    try:
        value, _ = decoder.raw_decode(text)
    except json.JSONDecodeError:
        return None

    if isinstance(value, dict) and set(REPAIR_DECISION_SCHEMA["required"]) <= value.keys():
        return value

    return None


def _decision_candidates(text: str) -> list[dict]:
    """Scan object starts in noisy model output, retaining their original order."""
    decoder = json.JSONDecoder()
    matches = []
    for offset, character in enumerate(text):
        if character != "{":
            continue

        value = _decoded_decision(text[offset:], decoder)
        if value is not None:
            matches.append(value)

    return matches


def parse_repair_decision(text: str) -> dict:
    """Validate the last schema-shaped object, never falling back past an invalid decision."""
    matches = _decision_candidates(text)
    if not matches:
        raise ValueError("model output contains no repair decision JSON")

    decision = matches[-1]
    if decision["action"] not in REPAIR_DECISION_SCHEMA["properties"]["action"]["enum"]:
        raise ValueError(f"unsupported repair action: {decision['action']}")

    if not all(isinstance(decision[key], str) for key in REPAIR_DECISION_SCHEMA["required"]):
        raise ValueError("repair decision values must be strings")

    return decision


def _validate_no_fix(decision: dict, evidence: str) -> dict:
    """Validate the evidence and fields required for the no_fix action."""
    if decision["subject"].strip() or decision["replacement"].strip():
        return {"result": "REJECTED", "error": "no_fix requires empty subject and replacement"}

    return {"result": "ACCEPTED", "error": ""}


def _validate_dependency_decision(decision: dict, evidence: str) -> dict:
    """Validate the evidence and fields required for the add_dependency action."""
    modules = re.findall(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)", evidence)
    package = decision["subject"].removeprefix("python3-").replace("-", "_").lower()
    supported = {module.split(".")[0].replace("-", "_").lower() for module in modules}
    if not supported or package not in supported:
        return {"result": "REJECTED", "error":
                f"add_dependency requires a matching ModuleNotFoundError; imports={sorted(supported)}"}

    if decision["replacement"].strip():
        return {"result": "REJECTED", "error": "add_dependency requires an empty replacement"}

    return {"result": "ACCEPTED", "error": ""}


def _argument_rejected(argument: str, rejected: list[str]) -> bool:
    """Require a nonempty argument occurring in an unrecognized-argument diagnostic."""
    return bool(argument) and any(argument in value for value in rejected)


def _validate_argument_decision(decision: dict, evidence: str) -> dict:
    """Validate the evidence and fields required for the remove_rule_argument action."""
    argument = decision["subject"].strip()
    rejected = re.findall(r"error:\s+unrecognized arguments?:\s*([^\\\n\"]+)", evidence)
    if not _argument_rejected(argument, rejected):
        return {"result": "REJECTED", "error":
                f"remove_rule_argument requires an exact unrecognized argument; errors={rejected[-3:]}"}

    if decision["replacement"].strip():
        return {"result": "REJECTED", "error": "remove_rule_argument requires an empty replacement"}

    return {"result": "ACCEPTED", "error": ""}


def _validate_quilt_decision(decision: dict, evidence: str) -> dict:
    """Require a safe patch name and evidence of its application failure."""
    action = decision["action"]
    name = decision["subject"].strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+~-]*\.patch", name):
        return {"result": "REJECTED", "error": f"unsafe quilt patch name: {name!r}"}

    if name not in evidence or not re.search(
            rf"(?is){re.escape(name)}.*(?:FAILED|does not apply|subprocess returned exit status)", evidence):
        return {"result": "REJECTED", "error": f"{action} requires a named patch application failure"}

    if decision["replacement"].strip():
        return {"result": "REJECTED", "error": f"{action} requires an empty replacement"}

    return {"result": "ACCEPTED", "error": ""}


def _packaging_path_error(*values: str) -> str:
    """Reject absolute, traversing, or empty replacement paths in argument order."""
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            return f"unsafe packaging path: {value!r}"

    return ""


def _validate_path_decision(decision: dict, evidence: str) -> dict:
    """Require an evidenced missing path and a distinct safe replacement path."""
    old, new = decision["subject"].strip(), decision["replacement"].strip()
    if not old or old not in evidence:
        return {"result": "REJECTED", "error": "replace_packaging_path requires the missing path from evidence"}

    error = _packaging_path_error(old, new)
    if error:
        return {"result": "REJECTED", "error": error}

    if old == new:
        return {"result": "REJECTED", "error": "replacement path is unchanged"}

    return {"result": "ACCEPTED", "error": ""}


def validate_repair_decision(decision: dict, evidence: str) -> dict:
    """Dispatch evidence validation while retaining the existing path-action fallback."""
    validators = {
        "no_fix": _validate_no_fix,
        "add_dependency": _validate_dependency_decision,
        "remove_rule_argument": _validate_argument_decision,
        "refresh_quilt_patch": _validate_quilt_decision,
        "drop_quilt_patch": _validate_quilt_decision,
    }
    validate = validators.get(decision["action"], _validate_path_decision)
    return validate(decision, evidence)


def _missing_imports(evidence: str) -> list[str]:
    """Extract missing-module names without treating unrelated model text as evidence."""
    return re.findall(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)", evidence)


def _import_root(module: str) -> str:
    """Normalize the top-level Python import name for Debian dependency matching."""
    return module.split(".")[0].replace("-", "_").lower()


def _normalize_dependency_decision(decision: dict, evidence: str) -> dict:
    """Correct one quoted import only when the actual failure evidence supports it."""
    normalized = dict(decision)
    quoted = list(dict.fromkeys(_missing_imports(decision.get("evidence", ""))))
    supported = set(map(_import_root, _missing_imports(evidence)))
    if len(quoted) != 1:
        return normalized

    module = _import_root(quoted[0])
    package_module = decision.get("subject", "").removeprefix("python3-").replace("-", "_").lower()
    if module in supported and package_module != module:
        normalized["subject"] = "python3-" + module.replace("_", "-")

    return normalized


def _safe_source_file(tree: Path, value: str) -> bool:
    """Require a safe relative path naming a regular nonsymlink source file."""
    path = PurePosixPath(value)
    return not _unsafe_relative_path(path) and (tree / path).is_file() and not (tree / path).is_symlink()


def _reversed_packaging_paths(tree: Path, rules: str, old: str, new: str) -> bool:
    """Recognize reversed fields only when packaging references the absent path once."""
    return rules.count(new) == 1 and not (tree / new).exists() and _safe_source_file(tree, old)


def _normalize_path_decision(decision: dict, tree: Path) -> dict:
    """Swap reversed path fields only with unambiguous rules and source-file evidence."""
    normalized = dict(decision)
    old = decision.get("subject", "").strip()
    new = decision.get("replacement", "").strip()
    rules_path = tree / "debian/rules"
    if not old or not new or not rules_path.is_file():
        return normalized

    rules = rules_path.read_text(errors="replace")
    # Packaging identifies the missing path; the source tree proves its replacement exists.
    if _reversed_packaging_paths(tree, rules, old, new):
        normalized["subject"], normalized["replacement"] = new, old

    return normalized


def normalize_repair_decision(decision: dict, tree: Path, evidence: str = "") -> dict:
    """Correct supported field mismatches without mutating the original model decision."""
    if decision.get("action") == "add_dependency":
        return _normalize_dependency_decision(decision, evidence)

    if decision.get("action") == "replace_packaging_path":
        return _normalize_path_decision(decision, tree)

    return dict(decision)


def _replace_file_patch(path: str, before: str, after: str) -> str:
    """Render a Git-formatted file diff, rejecting proposals with no content change."""
    if before == after:
        raise ValueError(f"repair did not change {path}")
    body = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))
    return f"diff --git a/{path} b/{path}\n{body}"


def _normalized_context(value: str) -> str:
    """Ignore horizontal whitespace differences when matching existing source lines."""
    return re.sub(r"[ \t]+", " ", value.strip())


def _hunk_payloads(section: str) -> list[list[str]]:
    """Split unified hunks into payload lines without interpreting their declared offsets."""
    headers = list(re.finditer(r"^@@\s+[^\n]+@@[^\n]*(?:\n|$)", section, re.M))
    ends = [header.start() for header in headers[1:]] + [len(section)]
    return [section[header.end():end].splitlines(keepends=True) for header, end in zip(headers, ends)]


def _hunk_operations(payload: list[str]) -> list[tuple[str, str]]:
    """Keep context and change lines, excluding unified-diff metadata markers."""
    return [(line[0], line[1:]) for line in payload if line and line[0] in {" ", "+", "-"}]


def _normalized_positions(lines: list[str], old: list[str]) -> list[int]:
    """Locate all complete whitespace-normalized matches of a nonempty old payload."""
    if not old:
        return []

    needle = list(map(_normalized_context, old))
    content = list(map(_normalized_context, lines))
    width = len(needle)
    return [start for start in range(len(content) - width + 1) if content[start:start + width] == needle]


def _replace_hunk(lines: list[str], start: int, operations: list[tuple[str, str]]) -> None:
    """Replay a matched hunk while retaining the source's unchanged context formatting."""
    cursor = start
    replacement = []
    for operation, value in operations:
        if operation == " ":
            replacement.append(lines[cursor])
            cursor += 1
        elif operation == "-":
            cursor += 1
        else:
            replacement.append(value)

    lines[start:cursor] = replacement


def _apply_normalized_hunk(lines: list[str], payload: list[str]) -> bool:
    """Apply one hunk only when its complete old side has a unique normalized match."""
    operations = _hunk_operations(payload)
    old = [value for operation, value in operations if operation in {" ", "-"}]
    positions = _normalized_positions(lines, old)
    if len(positions) != 1:
        return False

    _replace_hunk(lines, positions[0], operations)
    return True


def _apply_hunks_with_normalized_context(path: Path, section: str) -> bool:
    """Write refreshed content only after every hunk matches uniquely in sequence."""
    payloads = _hunk_payloads(section)
    if not payloads:
        return False

    lines = path.read_text(errors="replace").splitlines(keepends=True)
    for payload in payloads:
        if not _apply_normalized_hunk(lines, payload):
            return False

    path.write_text("".join(lines))
    return True


def _line_indent(value: str) -> int:
    """Count leading spaces and tabs using the existing textual indentation policy."""
    return len(value) - len(value.lstrip(" \t"))


def _removed_construct(removed: list[str], pattern: str) -> tuple[int, str] | None:
    """Require exactly one named construct in the removed payload."""
    constructs = []
    for value in removed:
        match = re.match(pattern, value)
        if match:
            constructs.append((len(match.group(1)), match.group(2)))

    return constructs[0] if len(constructs) == 1 else None


def _construct_start(lines: list[str], pattern: str) -> int | None:
    """Find a uniquely named, correctly indented construct in current source."""
    expression = re.compile(pattern)
    matches = [index for index, value in enumerate(lines) if expression.match(value.rstrip("\r\n"))]
    return matches[0] if len(matches) == 1 else None


def _decorator_start(lines: list[str], start: int, indent: int) -> int:
    """Include contiguous single-line decorators at the declaration's indentation."""
    while start and lines[start - 1].strip().startswith("@") and _line_indent(lines[start - 1]) == indent:
        start -= 1

    return start


def _declaration_end(lines: list[str], start: int, indent: int) -> int:
    """Stop before the next nonblank line at or above the declaration's indentation."""
    for end in range(start + 1, len(lines)):
        if lines[end].strip() and _line_indent(lines[end]) <= indent:
            return end

    return len(lines)


def _remove_python_declaration(lines: list[str], removed: list[str]) -> bool:
    """Remove one uniquely named Python declaration from a drifted test file."""
    construct = _removed_construct(removed, r"^(\s*)(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    if construct is None:
        return False

    indent, name = construct
    start = _construct_start(lines, rf"^\s{{{indent}}}(?:async\s+def|def|class)\s+{re.escape(name)}\b")
    if start is None:
        return False

    end = _declaration_end(lines, start, indent)
    start = _decorator_start(lines, start, indent)
    del lines[start:end]
    return True


def _assignment_end(lines: list[str], start: int) -> int | None:
    """Find the closing parenthesis using the existing textual balance heuristic."""
    balance = lines[start].count("(") - lines[start].count(")")
    end = start + 1
    while balance > 0 and end < len(lines):
        balance += lines[end].count("(") - lines[end].count(")")
        end += 1

    return end if balance == 0 else None


def _remove_python_assignment(lines: list[str], removed: list[str]) -> bool:
    """Remove one parenthesized assignment despite formatting-only drift."""
    construct = _removed_construct(
        [value.rstrip() for value in removed], r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\($")
    if construct is None:
        return False

    indent, name = construct
    start = _construct_start(lines, rf"^\s{{{indent}}}{re.escape(name)}\s*=\s*\(")
    if start is None:
        return False

    end = _assignment_end(lines, start)
    if end is None:
        return False

    del lines[start:end]
    return True


def _independent_groups(payload: list[str]) -> list[list[tuple[str, str]]]:
    """Split consecutive changed lines at context boundaries, ignoring file headers."""
    groups, current = [], []
    for value in payload:
        if _is_change_line(value):
            current.append((value[0], value[1:]))
        elif current:
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    return groups


def _is_change_line(value: str) -> bool:
    """Recognize added or removed payload lines without mistaking headers for edits."""
    return bool(value) and value[0] in {"+", "-"} and not value.startswith(("+++", "---"))


def _normalized_counts(lines: list[str]) -> Counter[str]:
    """Count nonblank normalized payload lines for existing-addition evidence."""
    return Counter(_normalized_context(value) for value in lines if value.strip())


class ChangeResult(Enum):
    """Distinguish a resolved edit from an already-present or unresolved change group."""

    APPLIED = "applied"
    ALREADY_PRESENT = "already-present"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class QuiltChangeGroup:
    """One contiguous set of removed and added lines within a drifted hunk."""

    removed: list[str]
    added: list[str]

    @classmethod
    def from_operations(cls, group: list[tuple[str, str]]) -> QuiltChangeGroup:
        """Separate old and new payloads while preserving their original line endings."""
        return cls([value for operation, value in group if operation == "-"],
                   [value for operation, value in group if operation == "+"])

    def already_present(self, lines: list[str]) -> bool:
        """Retain the existing policy requiring every normalized addition occurrence."""
        return _contains_payload(_normalized_counts(lines), _normalized_counts(self.added))

    def remove_python_construct(self, lines: list[str]) -> bool:
        """Permit declaration or assignment fallback only for a pure deletion group."""
        return not self.added and (_remove_python_declaration(lines, self.removed)
                                   or _remove_python_assignment(lines, self.removed))

    def apply(self, lines: list[str]) -> ChangeResult:
        """Prefer existing additions, then unique payload replacement, then deletion fallback."""
        if self.already_present(lines):
            return ChangeResult.ALREADY_PRESENT

        positions = _normalized_positions(lines, self.removed)
        if len(positions) == 1:
            start = positions[0]
            lines[start:start + len(self.removed)] = self.added
            return ChangeResult.APPLIED

        if self.remove_python_construct(lines):
            return ChangeResult.APPLIED

        return ChangeResult.UNRESOLVED


def _section_change_groups(section: str) -> list[QuiltChangeGroup]:
    """Collect independent edits in hunk order without using context as an anchor."""
    return [QuiltChangeGroup.from_operations(group)
            for payload in _hunk_payloads(section) for group in _independent_groups(payload)]


def _apply_independent_change_groups(path: Path, section: str) -> bool:
    """Commit separated edits only when every group is resolved and at least one changes."""
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    changed = False
    for group in _section_change_groups(section):
        result = group.apply(lines)
        if result is ChangeResult.UNRESOLVED:
            return False

        changed = changed or result is ChangeResult.APPLIED

    if changed:
        path.write_text("".join(lines))

    return changed


class QuiltPatchSuperseded(ValueError):
    """The failed patch's old side or target has disappeared upstream."""


@dataclass(frozen=True)
class QuiltSection:
    """One unified-diff section and its validated upstream target."""

    target: str
    text: str
    created: bool

    @classmethod
    def parse(cls, text: str, strip: int) -> QuiltSection:
        """Require a single old/new path pair before interpreting a section."""
        old = re.findall(r"^---\s+([^\t\n ]+)", text, re.M)
        new = re.findall(r"^\+\+\+\s+([^\t\n ]+)", text, re.M)
        if len(old) != 1 or len(new) != 1:
            raise ValueError("automatic quilt refresh requires one target per patch section")

        return cls(_quilt_target(new[0], strip), text, old[0] == "/dev/null")


def _quilt_target(value: str, strip: int) -> str:
    """Strip a patch path while excluding deleted, escaping, or packaging targets."""
    path = PurePosixPath(value)
    if value == "/dev/null" or path.is_absolute() or ".." in path.parts or len(path.parts) <= strip:
        raise ValueError(f"automatic quilt refresh does not support target: {value}")

    return _upstream_target(PurePosixPath(*path.parts[strip:]), value)


def _upstream_target(path: PurePosixPath, original: str) -> str:
    """Restrict mechanical refresh to nonempty paths outside Debian packaging."""
    if not path.parts or path.parts[0] == "debian":
        raise ValueError(f"automatic quilt refresh does not support target: {original}")

    return path.as_posix()


def _active_patch_words(line: str) -> list[str]:
    """Ignore series comments before tokenizing an active patch entry."""
    if line.lstrip().startswith('#'):
        return []

    return line.split()


def _active_quilt_entry(series: Path, name: str) -> list[str]:
    """Resolve exactly one active series entry for the requested patch."""
    entries = []
    for line in series.read_text().splitlines():
        words = _active_patch_words(line)
        if words and words[0] == name:
            entries.append(words)

    if len(entries) != 1:
        raise ValueError(f"expected one active {name!r} entry in debian/patches/series, found {len(entries)}")

    return entries[0]


def _quilt_strip(entry: list[str]) -> int:
    """Use the final explicit strip option, retaining quilt's default of one."""
    strip = 1
    for option in entry[1:]:
        if re.fullmatch(r"-p[0-9]", option):
            strip = int(option[2:])

    return strip


def _quilt_section_starts(text: str) -> list[int]:
    """Prefer Git section boundaries and fall back to traditional diff headers."""
    starts = [match.start() for match in re.finditer(r"^diff --git ", text, re.M)]
    if not starts:
        starts = [match.start() for match in re.finditer(
            r"^---\s+[^\t\n ]+(?:[ \t]+[^\n]*)?\n\+\+\+\s+[^\t\n ]+(?:[ \t]+[^\n]*)?$", text, re.M)]

    return starts


def _quilt_sections(text: str, strip: int) -> list[QuiltSection]:
    """Split Git or traditional unified diffs into validated file sections."""
    starts = _quilt_section_starts(text)
    if not starts:
        raise ValueError("automatic quilt refresh requires unified patch sections")

    ends = [*starts[1:], len(text)]
    return [QuiltSection.parse(text[start:end], strip) for start, end in zip(starts, ends)]


def _validate_quilt_targets(sections: list[QuiltSection]) -> None:
    """Bound refresh scope and prohibit repeated modifications to the same target."""
    targets = [section.target for section in sections]
    if len(set(targets)) != len(targets) or len(targets) > 32:
        raise ValueError("automatic quilt refresh requires 1-32 unique upstream targets")


def _patch_payload(text: str, prefix: str) -> Counter[str]:
    """Count added or removed lines without treating diff headers as payload."""
    return Counter(line[1:] for line in text.splitlines()
                   if line.startswith(prefix) and not line.startswith(prefix * 3))


def _contains_payload(content: Counter[str], payload: Counter[str]) -> bool:
    """Require a nonempty payload with every occurrence present in current source."""
    return bool(payload) and all(content[line] >= count for line, count in payload.items())


def _absent_payload(content: Counter[str], payload: Counter[str]) -> bool:
    """Require a nonempty payload whose lines are all absent from current source."""
    return bool(payload) and all(content[line] == 0 for line in payload)


@dataclass(frozen=True)
class QuiltPayload:
    """Line evidence used by the existing supersession and deletion policies."""

    content: Counter[str]
    additions: Counter[str]
    removals: Counter[str]
    nonempty_removals: Counter[str]

    @classmethod
    def read(cls, path: Path, section: QuiltSection) -> QuiltPayload:
        """Capture source and patch payload counts before attempting fallback repairs."""
        removals = _patch_payload(section.text, '-')
        return cls(Counter(path.read_text(errors='replace').splitlines()),
                   _patch_payload(section.text, '+'), removals,
                   Counter({line: count for line, count in removals.items() if line}))

    def mostly_upstream(self, upstream_provenance: bool) -> bool:
        """Retain the existing threshold for patches carrying upstream provenance."""
        total = sum(self.additions.values())
        matches = sum(min(self.content[line], count) for line, count in self.additions.items())
        return upstream_provenance and total >= 5 and matches / total >= 0.8

    def omission(self, upstream_provenance: bool) -> str | None:
        """Apply existing payload-based supersession decisions in priority order."""
        if _contains_payload(self.content, self.additions):
            return "patch additions are already present upstream"

        if self.mostly_upstream(upstream_provenance):
            return "most patch additions are already present in the upstream implementation"

        if _absent_payload(self.content, self.nonempty_removals):
            return "old patch payload no longer exists upstream"

        return None


def _delete_payload(path: Path, removals: Counter[str]) -> None:
    """Remove the requested occurrence counts after the caller proves they exist."""
    lines = path.read_text(errors='replace').splitlines(keepends=True)
    for removed, count in removals.items():
        for _ in range(count):
            index = next(i for i, line in enumerate(lines) if line.rstrip('\r\n') == removed)
            del lines[index]

    path.write_text(''.join(lines))


def _supporting_section(target: str) -> bool:
    """Recognize test and release-note paths used by the supporting-section policy."""
    parts = PurePosixPath(target).parts
    return 'tests' in parts or parts[:1] == ('releasenotes',)


@dataclass
class QuiltSectionRefresh:
    """Apply one section in an isolated source tree and report any omission reason."""

    working: Path
    section: QuiltSection
    patch_path: Path
    strip: int
    upstream_provenance: bool
    has_omissions: bool

    @property
    def target_path(self) -> Path:
        """Locate the validated section target inside the copied source tree."""
        return self.working / self.section.target

    def run_patch(self, *, dry_run: bool, reverse: bool = False) -> subprocess.CompletedProcess:
        """Run a bounded patch probe or application with explicit fuzz policy."""
        command = ['patch']
        if dry_run:
            command.append('--dry-run')

        command.extend(['--batch', '--forward', '--fuzz=2'])
        if reverse:
            command = ['patch', '--dry-run', '--batch', '--force', '--reverse', '--fuzz=0']

        command.extend([f'-p{self.strip}', '-i', str(self.patch_path)])
        return subprocess.run(command, cwd=self.working, text=True, capture_output=True, timeout=30)

    def initial_omission(self) -> str | None:
        """Handle removed targets and upstream ownership before running patch."""
        if not self.section.created and not self.target_path.exists():
            return 'upstream target was removed'

        self.validate_target()
        if self.section.created and self.target_path.exists():
            return self.created_omission()

        return None

    def validate_target(self) -> None:
        """Reject existing targets that are not ordinary files."""
        if self.target_path.exists() and (not self.target_path.is_file() or self.target_path.is_symlink()):
            raise ValueError(f'automatic quilt refresh requires a regular target: {self.section.target}')

    def created_omission(self) -> str:
        """Distinguish an incorporated file from a file now owned by upstream."""
        payload = QuiltPayload.read(self.target_path, self.section)
        if _contains_payload(payload.content, payload.additions):
            return 'created file is already present upstream'

        return 'upstream now owns the formerly packaging-created file'

    def apply(self) -> str | None:
        """Prefer ordinary application before classifying or repairing a failed section."""
        reason = self.initial_omission()
        if reason is not None:
            return reason

        completed = self.run_patch(dry_run=True)
        if completed.returncode == 0:
            self.apply_validated()
            return None

        if self.section.created:
            diagnostic = (completed.stdout + completed.stderr).strip()[-2000:]
            raise ValueError(f'new-file patch section for {self.section.target} cannot be refreshed: {diagnostic}')

        return self.repair_existing(completed)

    def apply_validated(self) -> None:
        """Apply a successful probe and reject any probe/application disagreement."""
        result = self.run_patch(dry_run=False)
        if result.returncode:
            raise ValueError(f'validated patch section failed to apply for {self.section.target}: '
                             + (result.stdout + result.stderr).strip()[-2000:])

    def repair_existing(self, completed: subprocess.CompletedProcess) -> str | None:
        """Try payload classification and context repair before reverse probing."""
        payload = QuiltPayload.read(self.target_path, self.section)
        reason = payload.omission(self.upstream_provenance)
        if reason is not None:
            return reason

        if _apply_hunks_with_normalized_context(self.target_path, self.section.text):
            return None

        if _apply_independent_change_groups(self.target_path, self.section.text):
            return None

        reverse = self.run_patch(dry_run=True, reverse=True)
        if reverse.returncode == 0:
            return 'patch is already present upstream'

        return self.final_fallback(payload, completed, reverse)

    def final_fallback(self, payload: QuiltPayload, completed: subprocess.CompletedProcess,
                       reverse: subprocess.CompletedProcess) -> str | None:
        """Handle pure deletions and drifted supporting sections, or report failure."""
        if not payload.additions:
            return self.deletion_fallback(payload, completed, reverse)

        return self.supporting_fallback(completed, reverse)

    def deletion_fallback(self, payload: QuiltPayload, completed: subprocess.CompletedProcess,
                          reverse: subprocess.CompletedProcess) -> str | None:
        """Retain pure-deletion behavior only when its complete payload is accounted for."""
        if _contains_payload(payload.content, payload.nonempty_removals):
            _delete_payload(self.target_path, payload.nonempty_removals)
            return None

        if _absent_payload(payload.content, payload.removals):
            return 'deleted payload is already absent upstream'

        return self.supporting_fallback(completed, reverse)

    def supporting_fallback(self, completed: subprocess.CompletedProcess,
                            reverse: subprocess.CompletedProcess) -> str:
        """Omit supporting sections only after an earlier section was omitted."""
        if self.has_omissions and _supporting_section(self.section.target):
            return 'supporting test or release-note section drifted after runtime sections were incorporated'

        diagnostic = (completed.stdout + completed.stderr + reverse.stdout + reverse.stderr).strip()[-2000:]
        raise ValueError(f'patch section for {self.section.target} cannot be refreshed or identified upstream: {diagnostic}')


def _quilt_original(path: Path) -> str | None:
    """Capture an existing regular target for rendering the refreshed diff."""
    if path.is_file() and not path.is_symlink():
        return path.read_text(errors='replace')

    return None


def _refreshed_section(target: str, original: str | None, working: Path) -> str:
    """Render an applied section with fresh three-line context."""
    after = (working / target).read_text(errors='replace')
    return ''.join(difflib.unified_diff(
        original.splitlines(keepends=True) if original is not None else [],
        after.splitlines(keepends=True),
        fromfile=f'a/{target}' if original is not None else '/dev/null',
        tofile=f'b/{target}', n=3))


def _render_refreshed_patch(original_patch: str, working: Path, before: dict[str, str | None],
                            applied: list[str], omitted: list[tuple[str, str]]) -> str:
    """Preserve the patch header and signal when no applied section changes source."""
    header = original_patch.split('--- ', 1)[0].rstrip()
    refreshed = [header + '\n' if header else '']
    for target in applied:
        diff = _refreshed_section(target, before[target], working)
        if diff:
            refreshed.append(diff)

    if len(refreshed) == 1:
        _raise_superseded(omitted)

    return ''.join(refreshed)


def _raise_superseded(omitted: list[tuple[str, str]]) -> None:
    """Report all omission evidence when the complete quilt patch has disappeared."""
    reasons = '; '.join(f'{target}: {reason}' for target, reason in omitted)
    raise QuiltPatchSuperseded(
        'mechanical quilt refresh found the complete patch superseded upstream'
        + (f': {reasons}' if reasons else ''))


def _apply_quilt_sections(sections: list[QuiltSection], working: Path, temporary: Path,
                          strip: int, upstream_provenance: bool) -> tuple[list[str], list[tuple[str, str]]]:
    """Apply sections in order while retaining evidence that informs later sections."""
    applied = []
    omitted = []
    for index, section in enumerate(sections):
        path = temporary / f'section-{index}.patch'
        path.write_text(section.text)
        refresh = QuiltSectionRefresh(working, section, path, strip, upstream_provenance, bool(omitted))
        reason = refresh.apply()
        if reason is None:
            applied.append(section.target)
        else:
            omitted.append((section.target, reason))

    return applied, omitted


def _read_quilt_patch(tree: Path, name: str) -> tuple[str, int]:
    """Require a present patch and series entry before interpreting its strip policy."""
    patch_path = tree / 'debian/patches' / name
    series_path = tree / 'debian/patches/series'
    if not patch_path.is_file() or patch_path.is_symlink() or not series_path.is_file():
        raise ValueError(f'quilt patch is missing: {name}')

    strip = _quilt_strip(_active_quilt_entry(series_path, name))
    return patch_path.read_text(errors='replace'), strip


def _refresh_quilt_patch(tree: Path, name: str) -> str:
    """Refresh applicable sections and omit sections already present upstream."""
    original_patch, strip = _read_quilt_patch(tree, name)
    upstream_provenance = bool(re.search(
        r'(?mi)^(?:Origin:\s*upstream(?:,|\s|$)|Applied-Upstream:|Change-Id:)', original_patch))
    sections = _quilt_sections(original_patch, strip)
    _validate_quilt_targets(sections)
    with tempfile.TemporaryDirectory(prefix='packagetest-quilt-refresh-') as temp:
        working = Path(temp) / 'source'
        shutil.copytree(tree, working, symlinks=True)
        before = {section.target: _quilt_original(working / section.target) for section in sections}
        applied, omitted = _apply_quilt_sections(sections, working, Path(temp), strip, upstream_provenance)
        revised = _render_refreshed_patch(original_patch, working, before, applied, omitted)

    return _replace_file_patch(f'debian/patches/{name}', original_patch, revised)


def _control_field_start(lines: list[str], field: str) -> int:
    """Locate the requested control field or report the missing dependency stanza."""
    start = next((index for index, line in enumerate(lines) if line.startswith(field + ":")), None)
    if start is None:
        raise ValueError(f"control paragraph has no {field} field")

    return start


def _control_field_end(lines: list[str], start: int) -> int:
    """Stop a folded control field at the next field or blank paragraph separator."""
    for end in range(start + 1, len(lines)):
        if not lines[end].startswith((" ", "\t")) or not lines[end].strip():
            return end

    return len(lines)


def _dependency_insert_position(lines: list[str], start: int, end: int, package: str) -> int:
    """Insert before substitutions or lexically later dependency continuation lines."""
    for index in range(start + 1, end):
        token = lines[index].strip().split(maxsplit=1)[0].rstrip(",")
        if token.startswith("${") or token.lower() > package:
            return index

    return end


def _add_control_dependency(text: str, field: str, package: str) -> str:
    """Add a missing dependency while preserving the surrounding control paragraph."""
    lines = text.splitlines(keepends=True)
    start = _control_field_start(lines, field)
    end = _control_field_end(lines, start)
    current = "".join(lines[start:end])
    if re.search(rf"(?<![A-Za-z0-9+.-]){re.escape(package)}(?![A-Za-z0-9+.-])", current):
        return text

    # The field always includes its header, so its last line exists even at EOF.
    if not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"

    insert = _dependency_insert_position(lines, start, end, package)
    lines.insert(insert, f" {package},\n")
    return "".join(lines)


def _render_argument_removal(decision: dict, tree: Path) -> str:
    """Remove one exact, safe command argument line from Debian rules."""
    argument = decision["subject"].strip()
    if not re.fullmatch(r"--[A-Za-z0-9][A-Za-z0-9 _=.+-]*", argument):
        raise ValueError(f"unsafe rule argument: {argument!r}")

    path = tree / "debian/rules"
    before = path.read_text()
    lines = before.splitlines(keepends=True)
    matching = [index for index, line in enumerate(lines) if line.strip().rstrip("\\").strip() == argument]
    if len(matching) != 1:
        raise ValueError(f"expected one exact {argument!r} line in debian/rules, found {len(matching)}")

    del lines[matching[0]]
    return _replace_file_patch("debian/rules", before, "".join(lines))


def _render_quilt_drop(decision: dict, tree: Path) -> str:
    """Comment exactly one active series entry with its supersession explanation."""
    name = decision["subject"].strip()
    path = tree / "debian/patches/series"
    before = path.read_text()
    lines = before.splitlines(keepends=True)
    matching = [index for index, line in enumerate(lines)
                if _series_line_matches(line, name)]
    if len(matching) != 1:
        raise ValueError(f"expected one active {name!r} entry in debian/patches/series, found {len(matching)}")

    index = matching[0]
    lines[index] = "# Superseded upstream after snapshot rebase: " + lines[index]
    return _replace_file_patch("debian/patches/series", before, "".join(lines))


def _render_path_replacement(decision: dict, tree: Path) -> str:
    """Replace one missing rules path with an existing regular source file."""
    old, new = decision["subject"].strip(), decision["replacement"].strip()
    replacement = tree / new
    if not replacement.is_file() or replacement.is_symlink():
        raise ValueError(f"replacement upstream path does not exist: {new}")

    path = tree / "debian/rules"
    before = path.read_text()
    if before.count(old) != 1:
        raise ValueError(f"expected one {old!r} reference in debian/rules, found {before.count(old)}")

    if (tree / old).exists():
        raise ValueError(f"original upstream path still exists: {old}")

    return _replace_file_patch("debian/rules", before, before.replace(old, new, 1))


def _render_dependency_addition(decision: dict, tree: Path) -> str:
    """Render build and, unless build-only, Python runtime dependency additions."""
    package = decision["subject"].strip().lower()
    if not re.fullmatch(r"python3-[a-z0-9][a-z0-9+.-]*", package):
        raise ValueError(f"unsupported dependency package: {package!r}")

    path = tree / "debian/control"
    before = path.read_text()
    paragraphs = re.split(r"(\n\s*\n)", before)
    paragraphs[0] = _add_control_dependency(
        paragraphs[0], "Build-Depends-Indep" if "Build-Depends-Indep:" in paragraphs[0] else "Build-Depends", package)
    if decision.get("scope") == "build":
        return _replace_file_patch("debian/control", before, "".join(paragraphs))

    _add_runtime_dependencies(paragraphs, package)
    return _replace_file_patch("debian/control", before, "".join(paragraphs))


def _series_line_matches(line: str, name: str) -> bool:
    """Match an active series entry while preserving its optional quilt flags."""
    return bool(line.split()) and not line.lstrip().startswith("#") and line.split()[0] == name


def _python_runtime_paragraph(paragraph: str) -> bool:
    """Identify Python 3 binary stanzas with an existing runtime dependency field."""
    return bool(re.search(r"(?m)^Package:\s+python3-", paragraph)) and "Depends:" in paragraph


def _add_runtime_dependencies(paragraphs: list[str], package: str) -> None:
    """Update eligible runtime stanzas and reject proposals that change none of them."""
    changed = False
    for index in range(2, len(paragraphs), 2):
        paragraph = paragraphs[index]
        if _python_runtime_paragraph(paragraph):
            updated = _add_control_dependency(paragraph, "Depends", package)
            changed = changed or updated != paragraph
            paragraphs[index] = updated

    if not changed:
        raise ValueError("no Python 3 binary package dependency stanza was updated")


def render_source_repair(decision: dict, tree: Path) -> str:
    """Render a narrow packaging proposal using the selected action's constraints."""
    action = decision["action"]
    if action == "no_fix":
        return ""

    if action == "refresh_quilt_patch":
        return _refresh_quilt_patch(tree, decision["subject"].strip())

    renderers = {
        "remove_rule_argument": _render_argument_removal,
        "drop_quilt_patch": _render_quilt_drop,
        "replace_packaging_path": _render_path_replacement,
    }
    render = renderers.get(action, _render_dependency_addition)
    return render(decision, tree)


def _response_patch(text: str) -> str:
    """Prefer the last wrapped patch, falling back to the final complete Git diff."""
    patch = _between(text, "BEGIN_PATCH", "END_PATCH").strip()
    if patch:
        return patch

    # llama-cli may echo the prompt or omit wrappers around an otherwise usable diff.
    candidates = re.findall(
        r"(?ms)^diff --git a/\S+ b/\S+.*?(?=^```|^END_PATCH\s*$|^Exiting\.\.\.\s*$|\Z)", text)
    return candidates[-1].strip() if candidates else ""


def _unfenced_patch(patch: str) -> str:
    """Remove optional Markdown fences and normalize nonempty patches to a final newline."""
    if patch.startswith("```diff"):
        patch = patch[7:]
    elif patch.startswith("```"):
        patch = patch[3:]

    if patch.endswith("```"):
        patch = patch[:-3]

    patch = patch.strip()
    return patch + "\n" if patch else ""


def parse_model_response(text: str) -> ModelResponse:
    """Read the diagnosis and normalize a wrapped or standalone patch response."""
    diagnosis = _between(text, "BEGIN_DIAGNOSIS", "END_DIAGNOSIS").strip()
    return ModelResponse(diagnosis=diagnosis, patch=_unfenced_patch(_response_patch(text)))


def _between(text: str, start: str, end: str) -> str:
    """Return the final complete marker-delimited block, or an empty string."""
    matches = list(re.finditer(rf"{start}\s*(.*?)\s*{end}", text, re.DOTALL))
    return matches[-1].group(1) if matches else ""


def _repository_patch_path(value: str) -> None:
    """Require a safe target in the repository's reviewed areas."""
    path = PurePosixPath(value)
    if _unsafe_relative_path(path):
        raise ValueError(f"unsafe patch path: {value}")

    if path.parts[0] not in ALLOWED_PATCH_ROOTS:
        raise ValueError(f"patch path is outside reviewed repository areas: {value}")

    if value.startswith(FORBIDDEN_PATCH_PREFIXES):
        raise ValueError(f"forbidden patch path: {value}")


def patch_paths(patch: str) -> list[str]:
    """Check all diff identities before enforcing repository path restrictions."""
    paths = _git_patch_paths(patch)
    for value in paths:
        _repository_patch_path(value)

    return paths


def validate_patch(patch: str, repository: Path) -> dict:
    """Apply the patch in a disposable copy; never execute changed content."""
    result = {"result": "REJECTED", "paths": [], "error": ""}
    if len(patch.encode()) > 256 * 1024:
        result["error"] = "patch exceeds 256 KiB"
        return result
    try:
        result["paths"] = patch_paths(patch)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    with tempfile.TemporaryDirectory(prefix="packagetest-ai-patch-") as temp:
        checkout = Path(temp) / "checkout"
        shutil.copytree(repository, checkout, ignore=shutil.ignore_patterns(
            ".git", ".venv", ".cache", "artifacts", "evidence", "failure-analysis", "__pycache__"))
        patch_path = Path(temp) / "proposal.patch"
        patch_path.write_text(patch)
        command = ["git", "apply", "--recount", "--whitespace=error-all", str(patch_path)]
        completed = subprocess.run(command, cwd=checkout, text=True, capture_output=True, timeout=30)
        if completed.returncode:
            result["error"] = (completed.stderr or completed.stdout).strip()[-4000:]
            return result
        result.update(result="APPLIES", error="", patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    return result


def _source_patch_path(value: str) -> None:
    """Limit a source repair target to safe Debian packaging paths excluding changelog."""
    path = PurePosixPath(value)
    if _unsafe_relative_path(path) or path.parts[0] == ".git":
        raise ValueError(f"unsafe source patch path: {value}")

    if path.parts[0] != "debian" or path == PurePosixPath("debian/changelog"):
        raise ValueError(f"automatic remediation is restricted to Debian packaging: {value}")


def _unsafe_relative_path(path: PurePosixPath) -> bool:
    """Identify absolute, traversing, or empty paths before accessing their components."""
    return path.is_absolute() or ".." in path.parts or not path.parts


def _git_patch_paths(patch: str) -> list[str]:
    """Require Git diff headers without renames or changed target paths."""
    paths = []
    for old, new in re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE):
        if old != new:
            raise ValueError("renames and path changes are not accepted")

        paths.append(new)

    if not paths:
        raise ValueError("response contains no git unified diff")

    return paths


def source_patch_paths(patch: str) -> list[str]:
    """Validate paths in a patch intended for one prepared source tree."""
    # Validate each header before continuing so rejection precedence remains stable.
    paths = []
    for old, new in re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE):
        if old != new:
            raise ValueError("renames and path changes are not accepted")

        _source_patch_path(new)
        paths.append(new)

    if not paths:
        raise ValueError("response contains no git unified diff")

    return paths


def _added_patch_lines(patch: str) -> list[str]:
    """Extract added content without interpreting outer diff file headers as payload."""
    return [line[1:] for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def _test_bypass_error(patch: str, paths: list[str]) -> str:
    """Reject added test-bypass markers and deletion of test-named files."""
    added = "\n".join(_added_patch_lines(patch))
    bypass = re.compile(
        r"(?im)(pytest\.mark\.skip|unittest\.skip|@skip|xfail|DEB_BUILD_OPTIONS.*nocheck|"
        r"override_dh_auto_test[^\n]*:\s*(?:true|:)|exit\s+0\s*(?:#.*)?$)")
    if bypass.search(added):
        return "patch attempts to skip or bypass tests"

    if "deleted file mode" in patch and any("test" in path.lower() for path in paths):
        return "patch deletes test content"

    return ""


def _packaging_content_error(patch: str, paths: list[str]) -> str:
    """Reject ownership changes, placeholder/log content, and binary staging writes."""
    forbidden_content = re.compile(
        r"(?im)^\+(?:Maintainer|Uploaders|XSBC-Original-Maintainer):|"
        r"your\.email@example\.com|your name|traceback \(most recent call last\):"
    )
    if forbidden_content.search(patch):
        return "patch changes package ownership metadata or contains placeholder/log content"

    if any(re.match(r"debian/[^/]+/usr/", path) for path in paths):
        return "patch writes into a binary-package staging directory"

    return ""


def _series_patch_names(patch: str) -> set[str]:
    """Select uncommented added patch names using the existing series-reference policy."""
    return {line.strip() for line in _added_patch_lines(patch)
            if not line.lstrip().startswith("#") and line.strip().endswith(".patch")}


def _series_reference_error(patch: str, paths: list[str], tree: Path) -> str:
    """Require added series references to exist already or be included in the proposal."""
    series_additions = _series_patch_names(patch)
    changed = set(paths)
    missing = sorted(name for name in series_additions
                     if f"debian/patches/{name}" not in changed and not (tree / "debian/patches" / name).is_file())
    if missing:
        return f"series references patch files that do not exist: {missing}"
    return ""


def _source_patch_policy_error(patch: str, paths: list[str], tree: Path) -> str:
    """Apply content restrictions in their established rejection order."""
    return (_test_bypass_error(patch, paths)
            or _packaging_content_error(patch, paths)
            or _series_reference_error(patch, paths, tree))


def _check_source_application(patch: str, paths: list[str], tree: Path, apply: bool) -> subprocess.CompletedProcess:
    """Run Git validation or application with nested quilt whitespace handled explicitly."""
    tree = tree.resolve()
    patch_path = (tree.parent / ".packagetest-remediation.patch").resolve()
    patch_path.write_text(patch)
    # A unified diff stored inside debian/patches legitimately contains a
    # single-space context marker for blank lines. In an outer remediation
    # diff that marker looks like newly-added trailing whitespace to git.
    whitespace = ("nowarn" if all(path.startswith("debian/patches/") for path in paths)
                  else "error-all")
    command = ["git", "apply", "--recount", f"--whitespace={whitespace}"]
    if not apply:
        command.append("--check")
    command.append(str(patch_path))
    completed = subprocess.run(command, cwd=tree, text=True, capture_output=True, timeout=30)
    patch_path.unlink(missing_ok=True)
    return completed


def _source_patch_result(patch: str, paths: list[str], tree: Path, apply: bool) -> dict:
    """Return policy rejection or application evidence without changing the report schema."""
    result = {"result": "REJECTED", "paths": paths, "error": ""}
    error = _source_patch_policy_error(patch, paths, tree)
    if error:
        result["error"] = error
        return result

    completed = _check_source_application(patch, paths, tree, apply)
    if completed.returncode:
        result["error"] = (completed.stderr or completed.stdout).strip()[-4000:]
        return result

    result.update(result="APPLIED" if apply else "APPLIES", error="",
                  patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    return result


def validate_source_patch(patch: str, tree: Path, *, apply: bool = False) -> dict:
    """Bound a source patch before checking its paths, content policies, and application."""
    result = {"result": "REJECTED", "paths": [], "error": ""}
    if len(patch.encode()) > 256 * 1024:
        result["error"] = "patch exceeds 256 KiB"
        return result

    try:
        paths = source_patch_paths(patch)
    except ValueError as exc:
        result["error"] = str(exc)
        return result

    return _source_patch_result(patch, paths, tree, apply)


EvidenceChunk = tuple[int, str, str]


def _file_evidence_chunks(path: Path) -> list[EvidenceChunk]:
    """Read an archive or a bounded text file after the caller excludes symlinks."""
    if tarfile.is_tarfile(path):
        return _tar_chunks(path)

    if path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= 512 * 1024:
        text = path.read_text(errors="replace")
        return [(_priority(path.as_posix()), path.as_posix(), _tail(text, 12_000))]

    return []


def _render_evidence_chunks(chunks: list[EvidenceChunk], limit: int) -> str:
    """Render priority-ordered evidence within the existing tail-truncation budget."""
    rendered = []
    remaining = limit
    for _, name, content in chunks:
        block = f"\n--- {name} ---\n{content.strip()}\n"
        if len(block) > remaining:
            block = block[-remaining:]

        if block:
            rendered.append(block)
            remaining -= len(block)

        if remaining <= 0:
            break

    return "".join(rendered)


def safe_evidence_text(evidence: Path, *, limit: int = 12_000) -> str:
    """Read bounded text from evidence bundles without extracting archive members."""
    chunks: list[EvidenceChunk] = []
    for path in sorted(evidence.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue

        chunks.extend(_file_evidence_chunks(path))

    chunks.sort(key=lambda item: (item[0], item[1]))
    return _render_evidence_chunks(chunks, limit)


def _tar_text_path(name: str) -> bool:
    """Allow only nontraversing relative archive paths with recognized text suffixes."""
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and Path(name).suffix.lower() in TEXT_SUFFIXES


def _eligible_tar_member(member: tarfile.TarInfo) -> bool:
    """Exclude links, oversized members, unsafe names, and nontext archive content."""
    return member.isfile() and member.size <= 512 * 1024 and _tar_text_path(member.name)


def _tar_member_chunk(archive: tarfile.TarFile, member: tarfile.TarInfo, path: Path) -> EvidenceChunk | None:
    """Read one eligible member directly into bounded evidence without filesystem extraction."""
    if not _eligible_tar_member(member):
        return None

    stream = archive.extractfile(member)
    if stream is None:
        return None

    text = stream.read().decode(errors="replace")
    return _priority(member.name), f"{path.name}:{member.name}", _tail(text, 12_000)


def _tar_chunks(path: Path) -> list[EvidenceChunk]:
    """Collect safe archive evidence, retaining a diagnostic if tar decoding fails."""
    chunks = []
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                chunk = _tar_member_chunk(archive, member, path)
                if chunk is not None:
                    chunks.append(chunk)

    except tarfile.TarError as exc:
        chunks.append((0, path.name, f"Unreadable evidence archive: {exc}"))

    return chunks


def _failure_evidence_name(name: str) -> bool:
    """Recognize result records and explicitly named errors or failures."""
    return name.endswith("result.json") or "error" in name or "failure" in name


def _execution_evidence_name(name: str) -> bool:
    """Recognize nightly build and autopkgtest execution logs."""
    return name.endswith("nightly-build.log") or "autopkgtest" in name


def _packaging_evidence_name(name: str) -> bool:
    """Recognize Debian packaging paths and source/build descriptors."""
    return "/debian/" in name or name.endswith((".dsc", ".build"))


def _priority(name: str) -> int:
    """Rank failure records before execution logs, packaging metadata, and other text."""
    lower = name.lower()
    if _failure_evidence_name(lower):
        return 0

    if _execution_evidence_name(lower):
        return 1

    if _packaging_evidence_name(lower):
        return 2

    return 5


def _tail(text: str, limit: int) -> str:
    """Retain a bounded text tail with an explicit marker when earlier content is omitted."""
    return text if len(text) <= limit else "[earlier output omitted]\n" + text[-limit:]


def repository_context(repository: Path, source: str, evidence: str = "", *, limit: int = 10_000) -> str:
    """Include frozen catalog metadata for the failed source and implicated producers."""
    sources = [source]
    missing_modules = {item.split(".")[0] for item in re.findall(r"No module named ['\"]([^'\"]+)", evidence)}
    catalog_path = repository / "nightly-plan" / "catalog.json"
    if missing_modules and catalog_path.is_file():
        try:
            packages = json.loads(catalog_path.read_text())["packages"]
            implicated = []
            for package in packages:
                names = [package["source"], *package.get("binaries", [])]
                normalized = {re.sub(r"^python3?-", "", name).replace("-", "_").replace(".", "_") for name in names}
                if any(module.replace("-", "_").replace(".", "_") in normalized for module in missing_modules):
                    implicated.append(package["source"])
            sources = list(dict.fromkeys([*implicated, source]))
        except (KeyError, TypeError, ValueError):
            pass
    if not catalog_path.is_file():
        return "\nNo frozen catalog metadata is available for this source.\n"
    try:
        packages = json.loads(catalog_path.read_text())["packages"]
        selected = [package for package in packages if package.get("source") in sources]
    except (KeyError, TypeError, ValueError):
        return "\nFrozen catalog metadata could not be read.\n"
    text = json.dumps(selected, indent=2, sort_keys=True)
    return text[:limit]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
