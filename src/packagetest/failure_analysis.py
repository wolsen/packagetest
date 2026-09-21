"""Bounded, advisory analysis of packaging failures by a local model."""
from __future__ import annotations

from dataclasses import dataclass
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
        "action": {"type": "string", "enum": ["add_dependency", "remove_rule_argument", "no_fix"]},
        "package": {"type": "string"},
        "argument": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["action", "package", "argument", "evidence"],
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


def _replace_file_patch(path: str, before: str, after: str) -> str:
    if before == after:
        raise ValueError(f"repair did not change {path}")
    body = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))
    return f"diff --git a/{path} b/{path}\n{body}"


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
    lines.insert(end, f" {package},\n")
    return "".join(lines)


def render_source_repair(decision: dict, tree: Path) -> str:
    """Render a narrow, trusted Debian packaging patch from a model decision."""
    action = decision["action"]
    if action == "no_fix":
        return ""
    if action == "remove_rule_argument":
        argument = decision["argument"].strip()
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
    package = decision["package"].strip().lower()
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
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip().endswith(".patch")
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
    command = ["git", "apply", "--recount", "--whitespace=error-all"]
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
