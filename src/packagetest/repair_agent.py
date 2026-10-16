"""Bound local-model investigation and render exact edits as reviewable Debian patches."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import object_mapping, records, required_text, text_field

MAX_RESPONSE_BYTES = 48 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_EDITS = 8
MAX_FILES = 5000
MAX_TOOL_OUTPUT = 8000
EXCLUDED = {".git", ".pc", ".tox", ".venv", "__pycache__", "build", "dist", ".cache"}
PROTECTED = {"debian/changelog", "debian/source/format"}


def _relative_path(name: str) -> PurePosixPath:
    """Reject escaping or ambiguous names before filesystem access or diff rendering."""
    if re.fullmatch(r"[A-Za-z0-9_.+/-]*", name) is None:
        raise ValueError(f"Unsafe repair path: {name!r}")

    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or _ambiguous_path(name, relative)
    ):
        raise ValueError(f"Unsafe repair path: {name!r}")

    return relative


def _ambiguous_path(name: str, relative: PurePosixPath) -> bool:
    """Allow the empty inspection root while requiring canonical names for every file."""
    return bool(name) and str(relative) != name


def _checked_path(root: Path, relative: PurePosixPath) -> Path:
    """Reject excluded directories and every symlink in a package-relative path."""
    if set(relative.parts) & EXCLUDED:
        raise ValueError(f"Excluded repair path: {relative}")

    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Symlink in repair path: {relative}")

    return path


def safe_path(root: Path, name: str, *, directory: bool = False) -> Path:
    """Resolve a package-relative path without traversing links or private directories."""
    relative = _relative_path(name)
    path = _checked_path(root, relative)
    if not directory and (not relative.parts or path.is_dir()):
        raise ValueError("Expected a package-relative file path")

    return path


def read_text(path: Path) -> str:
    """Read a bounded UTF-8 source file, rejecting binary and oversized input."""
    if not path.is_file():
        raise ValueError("Inspection requires a regular source file")

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Repair source file exceeds 256 KiB")

    text = path.read_text(encoding="utf-8")
    _validate_content(text)
    return text


def _validate_content(text: str) -> None:
    """Keep inspected and generated text within the same binary and size boundaries."""
    if "\x00" in text:
        raise ValueError("Binary files are outside the repair interface")

    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Repair source file exceeds 256 KiB")


def parse_response(output: str) -> dict[str, object]:
    """Read new protocol responses while retaining the existing decision protocol."""
    from .failure_analysis import parse_repair_decision

    if len(output.encode()) > MAX_RESPONSE_BYTES:
        raise ValueError("Model response exceeds 48 KiB")

    text = _strip_fences(output)

    try:
        value = object_mapping(
            json.loads(text, object_pairs_hook=_unique_object), "model response"
        )
    except json.JSONDecodeError:
        return dict(parse_repair_decision(output))

    if text_field(value, "action") in {"inspect", "propose_patch"}:
        return value

    return dict(parse_repair_decision(output))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON fields instead of silently accepting the last instruction."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate response field: {key}")

        result[key] = value

    return result


def _strip_fences(output: str) -> str:
    """Accept one conventional Markdown code fence without scanning arbitrary embedded objects."""
    text = output.strip()
    if text.startswith("```") and text.endswith("```"):
        return text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    return text


@dataclass(frozen=True)
class TextEdit:
    """One exact replacement or new text file; no model-generated commands are executed."""

    path: str
    before: str
    after: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TextEdit:
        """Require an explicit path and both replacement strings with no extra fields."""
        if set(value) != {"path", "before", "after"}:
            raise ValueError("Each edit requires exactly path, before, and after")

        edit = cls(
            required_text(value, "path"),
            text_field(value, "before"),
            text_field(value, "after"),
        )
        if edit.before == edit.after:
            raise ValueError("An edit must change its selected text")

        return edit


@dataclass(frozen=True)
class PatchPlan:
    """An evidence-bound diagnosis and a small set of exact source or packaging edits."""

    diagnosis: str
    evidence: str
    edits: tuple[TextEdit, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], evidence: str) -> PatchPlan:
        """Require a concrete failure quote and a bounded, unambiguous edit inventory."""
        if set(value) != {"action", "diagnosis", "evidence", "edits"}:
            raise ValueError(
                "propose_patch requires action, diagnosis, evidence, and edits"
            )

        if value["action"] != "propose_patch":
            raise ValueError("Expected propose_patch action")

        quote = required_text(value, "evidence")
        if quote not in evidence:
            raise ValueError(
                "Proposal evidence must quote the current failure verbatim"
            )

        edits = tuple(
            TextEdit.from_mapping(item) for item in records(value.get("edits"), "edits")
        )
        _validate_edits(edits)
        return cls(required_text(value, "diagnosis"), quote, edits)


def _validate_edits(edits: tuple[TextEdit, ...]) -> None:
    """Bound proposal scope and keep each file represented by exactly one edit."""
    if not 1 <= len(edits) <= MAX_EDITS:
        raise ValueError("A proposal requires 1-8 edited files")

    if len({edit.path for edit in edits}) != len(edits):
        raise ValueError("Use one edit per file with an unambiguous before block")


def _file_inventory(root: Path) -> list[str]:
    """Walk bounded source names without following links or enumerating private trees."""
    result: list[str] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = _visible_directories(directories)
        result.extend(_relative_names(root, Path(current), filenames))
        if len(result) > MAX_FILES:
            raise ValueError(
                "Inspection inventory exceeds 5000 files; use a narrower directory"
            )

    return result


def _visible_directories(names: list[str]) -> list[str]:
    """Keep traversal order stable while pruning generated and private directories."""
    return sorted(name for name in names if name not in EXCLUDED)


def _relative_names(root: Path, current: Path, names: list[str]) -> list[str]:
    """Expose relative file names without reading their contents."""
    return [str((current / name).relative_to(root)) for name in sorted(names)]


def _search_files(path: Path) -> tuple[Path, list[str]]:
    """Describe either one search file or a bounded directory inventory."""
    if path.is_dir():
        return path, _file_inventory(path)

    return path.parent, [path.name]


def _directory_names(path: Path) -> list[str]:
    """Bound directory enumeration and omit links before inspecting entry metadata."""
    names: list[str] = []
    for entry in path.iterdir():
        if entry.name in EXCLUDED or entry.is_symlink():
            continue

        names.append(_entry_name(entry))
        if len(names) > MAX_FILES:
            raise ValueError("Inspection directory exceeds 5000 entries")

    return sorted(names)


def _entry_name(entry: Path) -> str:
    """Mark directories in listings so the next request can narrow its scope."""
    return entry.name + "/" if entry.is_dir() else entry.name


def _positive_integer(
    request: Mapping[str, object], key: str, default: int, maximum: int
) -> int:
    """Reject booleans and out-of-range line requests instead of coercing JSON values."""
    value = request.get(key, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{key} must be an integer between 1 and {maximum}")

    return value


def _bounded_results(results: list[dict[str, object]]) -> str:
    """Keep tool feedback valid JSON and explicitly mark clipped output."""
    rendered = json.dumps(results, ensure_ascii=True)
    if len(rendered) <= MAX_TOOL_OUTPUT:
        return rendered

    preview = rendered[: MAX_TOOL_OUTPUT // 2]
    result = json.dumps({"truncated": True, "preview": preview})
    while len(result) > MAX_TOOL_OUTPUT:
        preview = preview[: len(preview) // 2]
        result = json.dumps({"truncated": True, "preview": preview})

    return result


def _validate_request(request: Mapping[str, object], operation: str) -> None:
    """Reject unknown capabilities and fields before touching package files."""
    schemas = {
        "list": {"op", "path", "start"},
        "read": {"op", "path", "start", "lines"},
        "search": {"op", "path", "query"},
    }
    if operation not in schemas or set(request) - schemas[operation]:
        raise ValueError("Supported inspection operations are list, read, and search")


class SourceInspector:
    """Expose only bounded package-file inspection, without shell or network capabilities."""

    def __init__(self, root: Path) -> None:
        """Bind the exact source view used to interpret the next proposal's preimages."""
        self.root = root.resolve()

    def inspect(self, decision: Mapping[str, object]) -> str:
        """Execute up to three explicit read-only requests and return bounded JSON evidence."""
        if (
            set(decision) != {"action", "requests"}
            or decision.get("action") != "inspect"
        ):
            raise ValueError("inspect requires exactly action and requests")

        requests = records(decision.get("requests"), "inspection requests")
        if not 1 <= len(requests) <= 3:
            raise ValueError("inspect requires 1-3 requests")

        results = [self._result(request) for request in requests]
        return _bounded_results(results)

    def _result(self, request: Mapping[str, object]) -> dict[str, object]:
        """Retain request failures as model feedback without broadening allowed capabilities."""
        try:
            return {"request": dict(request), "output": self._execute(request)}
        except (ValueError, OSError, UnicodeError) as exc:
            return {"request": dict(request), "error": str(exc)}

    def _execute(self, request: Mapping[str, object]) -> object:
        """Dispatch a known inspection operation after validating its source path."""
        operation = required_text(request, "op")
        _validate_request(request, operation)
        path = safe_path(
            self.root, text_field(request, "path"), directory=operation != "read"
        )
        if operation == "read":
            return self._read(path, request)

        if operation == "list":
            return self._list(path, request)

        if operation == "search":
            return self._search(path, required_text(request, "query"))

        raise ValueError("Supported inspection operations are list, read, and search")

    def _list(self, path: Path, request: Mapping[str, object]) -> dict[str, object]:
        """List one directory with paging so large sources remain navigable."""
        start = _positive_integer(request, "start", 1, MAX_FILES)
        names = _directory_names(path)
        end = start - 1 + 100
        return {"paths": names[start - 1 : end], "truncated": len(names) > end}

    def _read(self, path: Path, request: Mapping[str, object]) -> list[str]:
        """Return numbered source lines so the model can request a smaller exact preimage."""
        start = _positive_integer(request, "start", 1, 100000)
        count = _positive_integer(request, "lines", 80, 120)
        lines = read_text(path).splitlines()
        return [
            f"{index + 1}: {lines[index]}"
            for index in range(start - 1, min(len(lines), start - 1 + count))
        ]

    def _search(self, path: Path, query: str) -> list[dict[str, object]]:
        """Search literal text within bounded readable source files, retaining at most 30 hits."""
        if len(query) > 200:
            raise ValueError("Search query exceeds 200 characters")

        parent, names = _search_files(path)
        result: list[dict[str, object]] = []
        for name in names:
            result.extend(self._matches(parent, name, query))
            if len(result) >= 30:
                break

        return result[:30]

    def _matches(self, parent: Path, name: str, query: str) -> list[dict[str, object]]:
        """Skip unreadable and binary files while preserving safe relative match locations."""
        try:
            path = safe_path(parent, name)
            lines = read_text(path).splitlines()
        except (ValueError, OSError, UnicodeError):
            return []

        return [
            {
                "path": str(path.relative_to(self.root)),
                "line": index,
                "text": line[:240],
            }
            for index, line in enumerate(lines, 1)
            if query in line
        ][:30]


def materialize_source(tree: Path) -> None:
    """Apply existing quilt patches with Debian's source tool before interpreting upstream edits."""
    format_path = tree / "debian/source/format"
    if not format_path.is_file() or format_path.read_text().strip() != "3.0 (quilt)":
        raise ValueError("Upstream edits require Debian source format 3.0 (quilt)")

    result = subprocess.run(
        ["dpkg-source", "--before-build", str(tree.resolve())],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError(
            "Cannot materialize the current quilt stack: "
            + (result.stderr + result.stdout)[-2000:]
        )

    _verify_materialized_stack(tree)


def _series_names(text: str) -> list[str]:
    """Read active quilt entries while leaving option interpretation to Debian's tool."""
    return [
        line.split()[0]
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _verify_materialized_stack(tree: Path) -> None:
    """Detect dpkg-source's already-applied heuristic when no matching quilt state exists."""
    series = safe_path(tree, "debian/patches/series")
    expected = _series_names(read_text(series)) if series.exists() else []
    database = tree / ".pc/applied-patches"
    applied = _series_names(read_text(database)) if database.exists() else []
    if applied != expected:
        raise ValueError(
            "Current quilt stack is not completely materialized; repair the existing patches first"
        )


def _test_assertions(text: str) -> dict[str, int]:
    """Count existing named Python test assertions as a conservative regression guard."""
    return _named_tests(ast.parse(text), "")


def _named_tests(tree: ast.AST, prefix: str) -> dict[str, int]:
    """Keep class and function scope so identically named methods cannot hide deletions."""
    result: dict[str, int] = {}
    for node in ast.iter_child_nodes(tree):
        result.update(_node_tests(node, prefix))

    return result


def _node_tests(node: ast.AST, prefix: str) -> dict[str, int]:
    """Record qualified test names and descend through their enclosing definitions."""
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        name = prefix + node.name
        nested = _named_tests(node, name + ".")
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test"):
            nested[name] = sum(_is_assertion(child) for child in ast.walk(node))

        return nested

    return _named_tests(node, prefix)


def _is_assertion(node: ast.AST) -> bool:
    """Recognize Python assert statements and unittest-style assertion calls."""
    if isinstance(node, ast.Assert):
        return True

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("assert")
    )


def _is_test_file(name: str) -> bool:
    """Identify Python test modules whose existing coverage must remain active."""
    return name.endswith(".py") and "test" in name.lower()


def _lost_assertions(before: dict[str, int], after: dict[str, int]) -> bool:
    """Require every existing named test to retain at least its original assertions."""
    return any(after.get(name, -1) < count for name, count in before.items())


def _override_lines(text: str) -> list[str]:
    """Select active override declarations without treating comments as policy changes."""
    return [
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]


def _validate_test_edit(name: str, before: str, after: str) -> None:
    """Preserve named tests and assertion counts, requiring a new regression for changed tests."""
    if not _is_test_file(name):
        return

    old, new = _test_assertions(before), _test_assertions(after)
    if _lost_assertions(old, new):
        raise ValueError(
            "A repair cannot remove existing tests or reduce their assertion counts"
        )

    if before and not new.keys() - old.keys():
        raise ValueError("Changing existing tests requires an added regression test")


def _validate_override(name: str, before: str, after: str, evidence: str) -> None:
    """Allow only explained file-specific Lintian exceptions tied to the observed diagnostic."""
    if not name.endswith(".lintian-overrides"):
        return

    if not any(line.startswith("# ") for line in after.splitlines()):
        raise ValueError("Lintian overrides require an explanatory comment")

    for line in set(_override_lines(after)) - set(_override_lines(before)):
        _validate_override_line(line, evidence)


def _validate_override_line(line: str, evidence: str) -> None:
    """Reject broad exceptions and architecture-mismatch suppression even when a build would pass."""
    match = re.fullmatch(
        r"[a-z0-9+.-]+(?::| binary:) ([a-z0-9-]+) \[([a-zA-Z0-9_./+-]+)\]", line
    )
    if match is None:
        raise ValueError(
            "Lintian overrides must name one package, tag, and literal file context"
        )

    tag, path = match.groups()
    _relative_path(path)
    if tag == "arch-independent-package-contains-binary-or-object":
        raise ValueError(
            "Correct package architecture instead of overriding an architecture mismatch"
        )

    if f"{tag} [{path}]" not in evidence:
        raise ValueError(
            "Lintian override must match a tag and file in the current failure"
        )


def _editable_path(root: Path, name: str) -> Path:
    """Protect identity, quilt bookkeeping, and maintenance hooks from generic edits."""
    path = safe_path(root, name)
    if (
        name in PROTECTED
        or name.startswith("debian/source/")
        or name.startswith("debian/patches/")
    ):
        raise ValueError(
            "Use the trusted quilt renderer; source identity and bookkeeping are protected"
        )

    if re.search(
        r"(?:^|[.])(preinst|postinst|prerm|postrm|config|triggers)$", Path(name).name
    ):
        raise ValueError("Maintainer hooks are outside the generic repair interface")

    return path


def _edit_file(root: Path, edit: TextEdit, evidence: str) -> tuple[str | None, str]:
    """Apply exactly one anchored edit after file, test, and override policy checks."""
    path = _editable_path(root, edit.path)
    before = read_text(path) if path.is_file() else None
    after = _replacement(before, edit)
    _validate_content(after)

    _validate_test_edit(edit.path, before or "", after)
    _validate_override(edit.path, before or "", after, evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding="utf-8")
    return before, after


def _replacement(before: str | None, edit: TextEdit) -> str:
    """Require a unique preimage for replacements and an absent file for additions."""
    if before is None:
        if edit.before:
            raise ValueError(f"Missing preimage file: {edit.path}")

        return edit.after

    if not edit.before or before.count(edit.before) != 1:
        raise ValueError(f"Preimage must occur exactly once: {edit.path}")

    return before.replace(edit.before, edit.after, 1)


def _validate_final_newline(text: str) -> None:
    """Keep generated text patches unambiguous, without empty-file deletions."""
    if not text or not text.endswith("\n"):
        raise ValueError("Edited files must be nonempty and end in a newline")


def _file_diff(name: str, before: str | None, after: str) -> str:
    """Render a text-file addition or replacement with a canonical final newline."""
    _validate_final_newline(after)

    body = "".join(
        difflib.unified_diff(
            (before or "").splitlines(True),
            after.splitlines(True),
            fromfile="/dev/null" if before is None else "a/" + name,
            tofile="b/" + name,
        )
    )
    mode = "new file mode 100644\n" if before is None else ""
    return f"diff --git a/{name} b/{name}\n{mode}{body}" if body else ""


def _quilt_changes(
    plan: PatchPlan, tree: Path, edits: list[TextEdit]
) -> dict[str, tuple[str | None, str]]:
    """Convert upstream edits against the materialized stack into one deterministic new quilt patch."""
    materialize_source(tree)
    payload = "".join(
        _file_diff(edit.path, *_edit_file(tree, edit, plan.evidence)) for edit in edits
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    name = f"debian/patches/packagetest-repair-{digest}.patch"
    title = " ".join(plan.diagnosis.split())[:160]
    patch = (
        f"Description: {title}\nOrigin: other, packagetest local model proposal\nForwarded: no\n\n"
        + payload
    )
    patch_path = safe_path(tree, name)
    if patch_path.exists():
        raise ValueError("Generated quilt patch already exists")

    series = safe_path(tree, "debian/patches/series")
    before = read_text(series) if series.exists() else None
    after = (before or "").rstrip("\n") + "\n" + Path(name).name + "\n"
    return {name: (None, patch), "debian/patches/series": (before, after.lstrip("\n"))}


def render_edit_repair(decision: Mapping[str, object], tree: Path) -> str:
    """Render model edits in disposable storage, exposing only a Debian packaging patch."""
    plan = PatchPlan.from_mapping(decision, required_text(decision, "evidence"))
    with tempfile.TemporaryDirectory(prefix="packagetest-edits-") as temporary:
        working = Path(temporary) / "source"
        shutil.copytree(tree, working, symlinks=True)
        changes = _render_changes(plan, working)
        return "".join(
            _file_diff(name, before, after)
            for name, (before, after) in sorted(changes.items())
        )


def _render_changes(
    plan: PatchPlan, working: Path
) -> dict[str, tuple[str | None, str]]:
    """Collect packaging replacements and route upstream text changes through quilt."""
    packaging, upstream = _partition_edits(plan.edits)
    changes = _packaging_changes(plan, working, packaging)
    if upstream:
        changes.update(_quilt_changes(plan, working, upstream))

    return changes


def _partition_edits(
    edits: tuple[TextEdit, ...],
) -> tuple[list[TextEdit], list[TextEdit]]:
    """Separate direct packaging files from upstream changes that need quilt provenance."""
    return (
        [edit for edit in edits if edit.path.startswith("debian/")],
        [edit for edit in edits if not edit.path.startswith("debian/")],
    )


def _packaging_changes(
    plan: PatchPlan, tree: Path, edits: list[TextEdit]
) -> dict[str, tuple[str | None, str]]:
    """Validate direct packaging edits and retain their original and replacement bytes."""
    return {edit.path: _edit_file(tree, edit, plan.evidence) for edit in edits}
