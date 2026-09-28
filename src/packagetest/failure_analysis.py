"""Bounded, advisory analysis of packaging failures by a local model."""
from __future__ import annotations

from dataclasses import dataclass
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


def parse_repair_decision(text: str) -> dict:
    """Extract the last schema-shaped JSON object from noisy llama-cli output."""
    decoder = json.JSONDecoder()
    matches = []
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(REPAIR_DECISION_SCHEMA["required"]) <= value.keys():
            matches.append(value)
    if not matches:
        raise ValueError("model output contains no repair decision JSON")
    decision = matches[-1]
    if decision["action"] not in REPAIR_DECISION_SCHEMA["properties"]["action"]["enum"]:
        raise ValueError(f"unsupported repair action: {decision['action']}")
    if not all(isinstance(decision[key], str) for key in REPAIR_DECISION_SCHEMA["required"]):
        raise ValueError("repair decision values must be strings")
    return decision


def validate_repair_decision(decision: dict, evidence: str) -> dict:
    """Require the selected action and value to be directly supported by evidence."""
    action = decision["action"]
    if action == "no_fix":
        if decision["subject"].strip() or decision["replacement"].strip():
            return {"result": "REJECTED", "error": "no_fix requires empty subject and replacement"}
        return {"result": "ACCEPTED", "error": ""}
    if action == "add_dependency":
        modules = re.findall(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)", evidence)
        package = decision["subject"].removeprefix("python3-").replace("-", "_").lower()
        supported = {module.split(".")[0].replace("-", "_").lower() for module in modules}
        if not supported or package not in supported:
            return {"result": "REJECTED", "error":
                    f"add_dependency requires a matching ModuleNotFoundError; imports={sorted(supported)}"}
        if decision["replacement"].strip():
            return {"result": "REJECTED", "error": "add_dependency requires an empty replacement"}
        return {"result": "ACCEPTED", "error": ""}
    if action == "remove_rule_argument":
        argument = decision["subject"].strip()
        rejected = re.findall(r"error:\s+unrecognized arguments?:\s*([^\\\n\"]+)", evidence)
        if not rejected or not any(argument and argument in value for value in rejected):
            return {"result": "REJECTED", "error":
                    f"remove_rule_argument requires an exact unrecognized argument; errors={rejected[-3:]}"}
        if decision["replacement"].strip():
            return {"result": "REJECTED", "error": "remove_rule_argument requires an empty replacement"}
        return {"result": "ACCEPTED", "error": ""}
    if action in {"refresh_quilt_patch", "drop_quilt_patch"}:
        name = decision["subject"].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+~-]*\.patch", name):
            return {"result": "REJECTED", "error": f"unsafe quilt patch name: {name!r}"}
        if name not in evidence or not re.search(
                rf"(?is){re.escape(name)}.*(?:FAILED|does not apply|subprocess returned exit status)", evidence):
            return {"result": "REJECTED", "error": f"{action} requires a named patch application failure"}
        if decision["replacement"].strip():
            return {"result": "REJECTED", "error": f"{action} requires an empty replacement"}
        return {"result": "ACCEPTED", "error": ""}
    old, new = decision["subject"].strip(), decision["replacement"].strip()
    if not old or old not in evidence:
        return {"result": "REJECTED", "error": "replace_packaging_path requires the missing path from evidence"}
    for value in (old, new):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            return {"result": "REJECTED", "error": f"unsafe packaging path: {value!r}"}
    if old == new:
        return {"result": "REJECTED", "error": "replacement path is unchanged"}
    return {"result": "ACCEPTED", "error": ""}


def normalize_repair_decision(decision: dict, tree: Path) -> dict:
    """Correct an unambiguous old/new path reversal using the prepared tree."""
    normalized = dict(decision)
    if decision.get("action") != "replace_packaging_path":
        return normalized
    old = decision.get("subject", "").strip()
    new = decision.get("replacement", "").strip()
    rules_path = tree / "debian/rules"
    if not old or not new or not rules_path.is_file():
        return normalized
    rules = rules_path.read_text(errors="replace")

    def safe_regular_file(value: str) -> bool:
        path = PurePosixPath(value)
        return (not path.is_absolute() and ".." not in path.parts and bool(path.parts)
                and (tree / path).is_file() and not (tree / path).is_symlink())

    # The missing path is the one referenced by packaging but absent from the
    # source tree. The replacement must be an existing regular source file.
    if rules.count(new) == 1 and not (tree / new).exists() and safe_regular_file(old):
        normalized["subject"], normalized["replacement"] = new, old
    return normalized


def _replace_file_patch(path: str, before: str, after: str) -> str:
    if before == after:
        raise ValueError(f"repair did not change {path}")
    body = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))
    return f"diff --git a/{path} b/{path}\n{body}"


def _refresh_quilt_patch(tree: Path, name: str) -> str:
    """Refresh applicable sections and omit sections already present upstream."""
    patch_path = tree / "debian/patches" / name
    series_path = tree / "debian/patches/series"
    if not patch_path.is_file() or patch_path.is_symlink() or not series_path.is_file():
        raise ValueError(f"quilt patch is missing: {name}")
    entries = [line.split() for line in series_path.read_text().splitlines()
               if line.split() and not line.lstrip().startswith("#") and line.split()[0] == name]
    if len(entries) != 1:
        raise ValueError(f"expected one active {name!r} entry in debian/patches/series, found {len(entries)}")
    strip = 1
    for option in entries[0][1:]:
        if re.fullmatch(r"-p[0-9]", option):
            strip = int(option[2:])
    original_patch = patch_path.read_text(errors="replace")
    starts = [match.start() for match in re.finditer(r"^diff --git ", original_patch, re.M)]
    if not starts:
        raise ValueError("automatic quilt refresh requires git-style patch sections")
    sections = [original_patch[start:(starts[index + 1] if index + 1 < len(starts) else len(original_patch))]
                for index, start in enumerate(starts)]
    targets = []
    parsed = []
    for section in sections:
        matches = re.findall(r"^\+\+\+\s+(?:b/)?([^\t\n ]+)", section, re.M)
        if len(matches) != 1:
            raise ValueError("automatic quilt refresh requires one target per patch section")
        value = matches[0]
        path = PurePosixPath(value)
        if value == "/dev/null" or path.is_absolute() or ".." in path.parts or path.parts[0] == "debian":
            raise ValueError(f"automatic quilt refresh does not support target: {value}")
        targets.append(value)
        parsed.append((value, section))
    if len(set(targets)) != len(targets) or len(targets) > 8:
        raise ValueError("automatic quilt refresh requires 1-8 unique existing upstream targets")
    with tempfile.TemporaryDirectory(prefix="packagetest-quilt-refresh-") as temp:
        working = Path(temp) / "source"
        shutil.copytree(tree, working, symlinks=True)
        before = {name: (working / name).read_text(errors="replace") for name in targets
                  if (working / name).is_file() and not (working / name).is_symlink()}
        if len(before) != len(targets):
            raise ValueError("automatic quilt refresh requires every target to exist as a regular file")
        applied = []
        for index, (target, section) in enumerate(parsed):
            section_path = Path(temp) / f"section-{index}.patch"
            section_path.write_text(section)
            command = ["patch", "--batch", "--forward", "--fuzz=2", f"-p{strip}", "-i", str(section_path)]
            completed = subprocess.run(command, cwd=working, text=True, capture_output=True, timeout=30)
            if completed.returncode == 0:
                applied.append(target)
                continue
            reverse = subprocess.run(
                ["patch", "--dry-run", "--batch", "--force", "--reverse", "--fuzz=2",
                 f"-p{strip}", "-i", str(section_path)],
                cwd=working, text=True, capture_output=True, timeout=30,
            )
            if reverse.returncode == 0:
                continue
            # Context can drift enough that reverse application also fails.
            # Compare the hunk payload itself: every added line must already
            # exist and every removed line must be absent. This also handles
            # pure-deletion hunks such as dependencies removed upstream.
            content = Counter((working / target).read_text(errors="replace").splitlines())
            additions = Counter(line[1:] for line in section.splitlines()
                                if line.startswith("+") and not line.startswith("+++"))
            removals = Counter(line[1:] for line in section.splitlines()
                               if line.startswith("-") and not line.startswith("---"))
            nonempty_removals = Counter({line: count for line, count in removals.items() if line})
            if not additions and nonempty_removals \
                    and all(content[line] >= count for line, count in nonempty_removals.items()):
                lines = (working / target).read_text(errors="replace").splitlines(keepends=True)
                for removed, count in nonempty_removals.items():
                    for _ in range(count):
                        index = next(i for i, line in enumerate(lines) if line.rstrip("\r\n") == removed)
                        del lines[index]
                (working / target).write_text("".join(lines))
                applied.append(target)
                continue
            if additions and all(content[line] >= count for line, count in additions.items()) \
                    and all(content[line] == 0 for line in removals):
                continue
            if not additions and removals and all(content[line] == 0 for line in removals):
                continue
            diagnostic = (completed.stdout + completed.stderr + reverse.stdout + reverse.stderr).strip()[-2000:]
            raise ValueError(f"patch section for {target} cannot be refreshed or identified upstream: {diagnostic}")
        header = original_patch.split("--- ", 1)[0].rstrip()
        refreshed = [header + "\n" if header else ""]
        for target in applied:
            after = (working / target).read_text(errors="replace")
            diff = "".join(difflib.unified_diff(
                before[target].splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{target}", tofile=f"b/{target}", n=3,
            ))
            if diff:
                refreshed.append(diff)
        revised = "".join(refreshed)
        if not any(block.startswith("--- a/") for block in refreshed):
            raise ValueError("mechanical quilt refresh produced no upstream change")
        return _replace_file_patch(f"debian/patches/{name}", original_patch, revised)


def _add_control_dependency(text: str, field: str, package: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith(field + ":")), None)
    if start is None:
        raise ValueError(f"control paragraph has no {field} field")
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        if not lines[end].strip():
            break
        end += 1
    current = "".join(lines[start:end])
    if re.search(rf"(?<![A-Za-z0-9+.-]){re.escape(package)}(?![A-Za-z0-9+.-])", current):
        return text
    if end and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    insert = end
    for index in range(start + 1, end):
        token = lines[index].strip().split(maxsplit=1)[0].rstrip(",") if lines[index].strip() else ""
        if token.startswith("${") or token.lower() > package:
            insert = index
            break
    lines.insert(insert, f" {package},\n")
    return "".join(lines)


def render_source_repair(decision: dict, tree: Path) -> str:
    """Render a narrow, trusted Debian packaging patch from a model decision."""
    action = decision["action"]
    if action == "no_fix":
        return ""
    if action == "remove_rule_argument":
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
    if action == "refresh_quilt_patch":
        return _refresh_quilt_patch(tree, decision["subject"].strip())
    if action == "drop_quilt_patch":
        name = decision["subject"].strip()
        path = tree / "debian/patches/series"
        before = path.read_text()
        lines = before.splitlines(keepends=True)
        matching = [index for index, line in enumerate(lines)
                    if line.split() and not line.lstrip().startswith("#") and line.split()[0] == name]
        if len(matching) != 1:
            raise ValueError(f"expected one active {name!r} entry in debian/patches/series, found {len(matching)}")
        index = matching[0]
        lines[index] = "# Superseded upstream after snapshot rebase: " + lines[index]
        return _replace_file_patch("debian/patches/series", before, "".join(lines))
    if action == "replace_packaging_path":
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
    package = decision["subject"].strip().lower()
    if not re.fullmatch(r"python3-[a-z0-9][a-z0-9+.-]*", package):
        raise ValueError(f"unsupported dependency package: {package!r}")
    path = tree / "debian/control"
    before = path.read_text()
    paragraphs = re.split(r"(\n\s*\n)", before)
    paragraphs[0] = _add_control_dependency(
        paragraphs[0], "Build-Depends-Indep" if "Build-Depends-Indep:" in paragraphs[0] else "Build-Depends", package)
    changed_runtime = False
    for index in range(2, len(paragraphs), 2):
        paragraph = paragraphs[index]
        if re.search(r"(?m)^Package:\s+python3-", paragraph) and "Depends:" in paragraph:
            updated = _add_control_dependency(paragraph, "Depends", package)
            changed_runtime = changed_runtime or updated != paragraph
            paragraphs[index] = updated
    if not changed_runtime:
        raise ValueError("no Python 3 binary package dependency stanza was updated")
    return _replace_file_patch("debian/control", before, "".join(paragraphs))


def parse_model_response(text: str) -> ModelResponse:
    """Parse a deliberately simple format that small local models can follow."""
    diagnosis = _between(text, "BEGIN_DIAGNOSIS", "END_DIAGNOSIS").strip()
    patch = _between(text, "BEGIN_PATCH", "END_PATCH").strip()
    # llama-cli can echo the prompt and small models sometimes omit the requested
    # wrapper while still returning one usable diff. Prefer the wrapper, but keep
    # the final complete diff as a conservative fallback.
    if not patch:
        candidates = re.findall(
            r"(?ms)^diff --git a/\S+ b/\S+.*?(?=^```|^END_PATCH\s*$|^Exiting\.\.\.\s*$|\Z)",
            text,
        )
        if candidates:
            patch = candidates[-1].strip()
    if patch.startswith("```diff"):
        patch = patch[7:]
    elif patch.startswith("```"):
        patch = patch[3:]
    if patch.endswith("```"):
        patch = patch[:-3]
    patch = patch.strip()
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return ModelResponse(diagnosis=diagnosis, patch=patch)


def _between(text: str, start: str, end: str) -> str:
    matches = list(re.finditer(rf"{start}\s*(.*?)\s*{end}", text, re.DOTALL))
    return matches[-1].group(1) if matches else ""


def patch_paths(patch: str) -> list[str]:
    paths = []
    for old, new in re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE):
        if old != new:
            raise ValueError("renames and path changes are not accepted")
        paths.append(new)
    if not paths:
        raise ValueError("response contains no git unified diff")
    for value in paths:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"unsafe patch path: {value}")
        if path.parts[0] not in ALLOWED_PATCH_ROOTS:
            raise ValueError(f"patch path is outside reviewed repository areas: {value}")
        if value.startswith(FORBIDDEN_PATCH_PREFIXES):
            raise ValueError(f"forbidden patch path: {value}")
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


def source_patch_paths(patch: str) -> list[str]:
    """Validate paths in a patch intended for one prepared source tree."""
    paths = []
    for old, new in re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE):
        if old != new:
            raise ValueError("renames and path changes are not accepted")
        path = PurePosixPath(new)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] == ".git":
            raise ValueError(f"unsafe source patch path: {new}")
        if path.parts[0] != "debian" or path == PurePosixPath("debian/changelog"):
            raise ValueError(f"automatic remediation is restricted to Debian packaging: {new}")
        paths.append(new)
    if not paths:
        raise ValueError("response contains no git unified diff")
    return paths


def validate_source_patch(patch: str, tree: Path, *, apply: bool = False) -> dict:
    """Check a model patch against prepared source and reject test bypasses."""
    result = {"result": "REJECTED", "paths": [], "error": ""}
    if len(patch.encode()) > 256 * 1024:
        result["error"] = "patch exceeds 256 KiB"
        return result
    try:
        result["paths"] = source_patch_paths(patch)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    added = "\n".join(line[1:] for line in patch.splitlines()
                       if line.startswith("+") and not line.startswith("+++"))
    bypass = re.compile(
        r"(?im)(pytest\.mark\.skip|unittest\.skip|@skip|xfail|DEB_BUILD_OPTIONS.*nocheck|"
        r"override_dh_auto_test[^\n]*:\s*(?:true|:)|exit\s+0\s*(?:#.*)?$)")
    if bypass.search(added):
        result["error"] = "patch attempts to skip or bypass tests"
        return result
    if "deleted file mode" in patch and any("test" in path.lower() for path in result["paths"]):
        result["error"] = "patch deletes test content"
        return result
    forbidden_content = re.compile(
        r"(?im)^\+(?:Maintainer|Uploaders|XSBC-Original-Maintainer):|"
        r"your\.email@example\.com|your name|traceback \(most recent call last\):"
    )
    if forbidden_content.search(patch):
        result["error"] = "patch changes package ownership metadata or contains placeholder/log content"
        return result
    if any(re.match(r"debian/[^/]+/usr/", path) for path in result["paths"]):
        result["error"] = "patch writes into a binary-package staging directory"
        return result
    series_additions = {
        line[1:].strip() for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
        and not line[1:].lstrip().startswith("#") and line[1:].strip().endswith(".patch")
    }
    changed = set(result["paths"])
    missing = sorted(name for name in series_additions
                     if f"debian/patches/{name}" not in changed and not (tree / "debian/patches" / name).is_file())
    if missing:
        result["error"] = f"series references patch files that do not exist: {missing}"
        return result
    tree = tree.resolve()
    patch_path = (tree.parent / ".packagetest-remediation.patch").resolve()
    patch_path.write_text(patch)
    # A unified diff stored inside debian/patches legitimately contains a
    # single-space context marker for blank lines. In an outer remediation
    # diff that marker looks like newly-added trailing whitespace to git.
    whitespace = ("nowarn" if all(path.startswith("debian/patches/") for path in result["paths"])
                  else "error-all")
    command = ["git", "apply", "--recount", f"--whitespace={whitespace}"]
    if not apply:
        command.append("--check")
    command.append(str(patch_path))
    completed = subprocess.run(command, cwd=tree, text=True, capture_output=True, timeout=30)
    patch_path.unlink(missing_ok=True)
    if completed.returncode:
        result["error"] = (completed.stderr or completed.stdout).strip()[-4000:]
        return result
    result.update(result="APPLIED" if apply else "APPLIES", error="",
                  patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    return result


def safe_evidence_text(evidence: Path, *, limit: int = 12_000) -> str:
    """Read bounded text from evidence bundles without extracting archive members."""
    chunks: list[tuple[int, str, str]] = []
    for path in sorted(evidence.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if tarfile.is_tarfile(path):
            chunks.extend(_tar_chunks(path))
        elif path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= 512 * 1024:
            text = path.read_text(errors="replace")
            chunks.append((_priority(path.as_posix()), path.as_posix(), _tail(text, 12_000)))
    chunks.sort(key=lambda item: (item[0], item[1]))
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


def _tar_chunks(path: Path) -> list[tuple[int, str, str]]:
    chunks = []
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                member_path = PurePosixPath(member.name)
                if not member.isfile() or member.size > 512 * 1024:
                    continue
                if member_path.is_absolute() or ".." in member_path.parts:
                    continue
                if Path(member.name).suffix.lower() not in TEXT_SUFFIXES:
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                text = stream.read().decode(errors="replace")
                name = f"{path.name}:{member.name}"
                chunks.append((_priority(member.name), name, _tail(text, 12_000)))
    except tarfile.TarError as exc:
        chunks.append((0, path.name, f"Unreadable evidence archive: {exc}"))
    return chunks


def _priority(name: str) -> int:
    lower = name.lower()
    if lower.endswith("result.json") or "error" in lower or "failure" in lower:
        return 0
    if lower.endswith("nightly-build.log") or "autopkgtest" in lower:
        return 1
    if "/debian/" in lower or lower.endswith((".dsc", ".build")):
        return 2
    return 5


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "[earlier output omitted]\n" + text[-limit:]


def repository_context(repository: Path, source: str, evidence: str = "", *, limit: int = 10_000) -> str:
    """Include the failed source and producers implicated by missing imports."""
    sources = [source]
    missing_modules = {item.split(".")[0] for item in re.findall(r"No module named ['\"]([^'\"]+)", evidence)}
    catalog_path = repository / "config" / "hibiscus-catalog.json"
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
    chunks = []
    for candidate in sources:
        root = repository / "config" / "patches" / candidate
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024:
                    chunks.append(f"\n--- {path.relative_to(repository)} ---\n{path.read_text(errors='replace')}\n")
    text = "".join(chunks)
    return text[:limit] if text else "\nNo existing repository adaptation exists for this source.\n"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
