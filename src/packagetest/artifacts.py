"""Validate Debian artifacts, including checksums and actual binary metadata."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fields(path: Path) -> dict[str, str]:
    """Read the first deb822 paragraph, optionally wrapped in a clear signature."""
    text = path.read_text()
    if text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        text = text.split("\n\n", 1)[1].split("-----BEGIN PGP SIGNATURE-----", 1)[0]
        text = "\n".join(line[2:] if line.startswith("- ") else line for line in text.splitlines())
    result: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line:
            if result:
                break
            continue
        if line[0].isspace() and key:
            result[key] += "\n" + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            if key in result:
                raise ValueError(f"Duplicate field {key} in {path}")
            result[key] = value.strip()
        else:
            raise ValueError(f"Invalid Debian metadata in {path}: {line}")
    return result


def checksum_entries(metadata: dict[str, str]) -> list[tuple[str, int, str]]:
    entries = []
    for line in metadata.get("Checksums-Sha256", "").splitlines():
        if not line.strip():
            continue
        digest, size, name = line.split()
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"Invalid checksum entry: {line}")
        if int(size) < 0:
            raise ValueError("Negative artifact size")
        entries.append((digest, int(size), name))
    if not entries or len({name for _, _, name in entries}) != len(entries):
        raise ValueError("Missing or duplicate Checksums-Sha256 entries")
    return entries


def verify_references(path: Path) -> dict[str, str]:
    metadata = fields(path)
    for digest, size, name in checksum_entries(metadata):
        artifact = path.parent / name
        if not artifact.is_file() or artifact.stat().st_size != size or sha256(artifact) != digest:
            raise ValueError(f"Missing or corrupt artifact referenced by {path.name}: {name}")
    return metadata


def verify_source(dsc: Path, source: str, version: str) -> dict[str, str]:
    metadata = verify_references(dsc)
    if metadata.get("Source") != source or metadata.get("Version") != version:
        raise ValueError(f"Source identity mismatch in {dsc.name}")
    return metadata


def verify_binaries(directory: Path, *, source: str, version: str, expected: list[str], arch: str) -> dict:
    changes = list(directory.glob("*.changes"))
    binary_changes = [p for p in changes if any(n.endswith(".deb") for _, _, n in checksum_entries(fields(p)))]
    if len(binary_changes) != 1:
        raise ValueError(f"Expected one binary .changes file, found {len(binary_changes)}")
    metadata = verify_references(binary_changes[0])
    if metadata.get("Source", "").split(" ", 1)[0] != source or metadata.get("Version") != version:
        raise ValueError("Binary .changes source/version mismatch")
    entries = checksum_entries(metadata)
    referenced_debs = {name for _, _, name in entries if name.endswith(".deb")}
    if referenced_debs != {p.name for p in directory.glob("*.deb")}:
        raise ValueError("Binary output set differs from .changes")
    binaries = []
    for name in sorted(referenced_debs):
        path = directory / name
        text = subprocess.check_output(["dpkg-deb", "--field", str(path)], text=True)
        control = {}
        for line in text.splitlines():
            if line and not line[0].isspace() and ":" in line:
                k, v = line.split(":", 1)
                control[k] = v.strip()
        actual_source = control.get("Source", control.get("Package", "")).split(" ", 1)[0]
        if actual_source != source or control.get("Version") != version or control.get("Architecture") not in {arch, "all"}:
            raise ValueError(f"Binary metadata mismatch: {name}")
        binaries.append({"file": name, "package": control["Package"], "version": control["Version"],
                         "architecture": control["Architecture"], "sha256": sha256(path)})
    if not binaries or not set(expected).issubset({b["package"] for b in binaries}):
        raise ValueError(f"Expected binaries not produced: {expected}")
    buildinfos = [directory / name for _, _, name in entries if name.endswith(".buildinfo")]
    if len(buildinfos) != 1:
        raise ValueError("Expected a checksummed .buildinfo file")
    info = fields(buildinfos[0])
    if info.get("Version") != version or info.get("Source", "").split(" ", 1)[0] != source:
        raise ValueError("Buildinfo source/version mismatch")
    installed = info.get("Installed-Build-Depends", "")
    versions = dict(re.findall(r"([a-z0-9][a-z0-9+.:\-]*)\s+\(=\s*([^\s)]+)\)", installed))
    if not versions:
        raise ValueError("Buildinfo has no installed build dependency versions")
    return {"binaries": binaries, "changes": binary_changes[0].name, "buildinfo": buildinfos[0].name,
            "build_dependency_versions": versions}
