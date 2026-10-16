"""Validate Debian artifacts, including checksums and actual binary metadata."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    """Compute an artifact's SHA-256 digest without loading it entirely into memory."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _unsigned_text(text: str) -> str:
    """Unwrap clear-signed metadata and remove its dash escaping without verifying it."""
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        return text

    text = text.split("\n\n", 1)[1].split("-----BEGIN PGP SIGNATURE-----", 1)[0]
    return "\n".join(line[2:] if line.startswith("- ") else line for line in text.splitlines())


class MetadataParagraph:
    """Accumulate deb822 fields while retaining the current continuation field."""

    def __init__(self, path: Path) -> None:
        """Retain the origin for diagnostics and initialize an empty paragraph."""
        self.path = path
        self.values: dict[str, str] = {}
        self.key: str | None = None

    def append(self, line: str) -> None:
        """Append a continuation or introduce a unique field from a nonempty line."""
        if line[0].isspace() and self.key:
            self.values[self.key] += "\n" + line.strip()
            return

        if ":" not in line:
            raise ValueError(f"Invalid Debian metadata in {self.path}: {line}")

        key, value = line.split(":", 1)
        if key in self.values:
            raise ValueError(f"Duplicate field {key} in {self.path}")

        self.key = key
        self.values[key] = value.strip()

    def consume(self, line: str) -> bool:
        """Consume a line, returning whether the first nonempty paragraph has ended."""
        # Comments neither terminate paragraphs nor extend a folded field.
        if line.startswith("#"):
            return False

        if not line:
            return bool(self.values)

        self.append(line)
        return False


def fields(path: Path) -> dict[str, str]:
    """Read the first deb822 paragraph, optionally wrapped in a clear signature."""
    paragraph = MetadataParagraph(path)
    for line in _unsigned_text(path.read_text()).splitlines():
        if paragraph.consume(line):
            break

    return paragraph.values


def _checksum_entry(line: str) -> tuple[str, int, str]:
    """Parse one SHA-256 record, rejecting unsafe names and negative sizes."""
    digest, size, name = line.split()
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"Invalid checksum entry: {line}")

    if int(size) < 0:
        raise ValueError("Negative artifact size")

    return digest, int(size), name


def checksum_entries(metadata: dict[str, str]) -> list[tuple[str, int, str]]:
    """Require a nonempty SHA-256 manifest with unique artifact names."""
    lines = filter(str.strip, metadata.get("Checksums-Sha256", "").splitlines())
    entries = [_checksum_entry(line) for line in lines]
    if not entries or len({name for _, _, name in entries}) != len(entries):
        raise ValueError("Missing or duplicate Checksums-Sha256 entries")

    return entries


def _verify_reference(path: Path, digest: str, size: int, name: str) -> None:
    """Check one referenced artifact's presence, size, and digest."""
    artifact = path.parent / name
    if not artifact.is_file() or artifact.stat().st_size != size or sha256(artifact) != digest:
        raise ValueError(f"Missing or corrupt artifact referenced by {path.name}: {name}")


def verify_references(path: Path) -> dict[str, str]:
    """Validate all checksummed files relative to their metadata descriptor."""
    metadata = fields(path)
    for digest, size, name in checksum_entries(metadata):
        _verify_reference(path, digest, size, name)

    return metadata


def verify_source(dsc: Path, source: str, version: str) -> dict[str, str]:
    """Require a complete source artifact set with the requested source identity."""
    metadata = verify_references(dsc)
    if metadata.get("Source") != source or metadata.get("Version") != version:
        raise ValueError(f"Source identity mismatch in {dsc.name}")

    return metadata


def _referenced_debs(metadata: dict[str, str]) -> set[str]:
    """Select binary package filenames from a validated checksum manifest."""
    return {name for _, _, name in checksum_entries(metadata) if name.endswith(".deb")}


def _binary_changes(directory: Path) -> tuple[Path, dict[str, str]]:
    """Require exactly one binary changes descriptor and verify its referenced files."""
    changes = [path for path in directory.glob("*.changes") if _referenced_debs(fields(path))]
    if len(changes) != 1:
        raise ValueError(f"Expected one binary .changes file, found {len(changes)}")

    return changes[0], verify_references(changes[0])


def _verify_identity(metadata: dict[str, str], source: str, version: str, message: str) -> None:
    """Match source/version metadata, permitting a parenthesized source version."""
    if metadata.get("Source", "").split(" ", 1)[0] != source or metadata.get("Version") != version:
        raise ValueError(message)


def _binary_control(path: Path) -> dict[str, str]:
    """Read top-level binary control fields, excluding folded continuation text."""
    text = subprocess.check_output(["dpkg-deb", "--field", str(path)], text=True)
    control = {}
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            control[key] = value.strip()

    return control


def _binary_record(path: Path, source: str, version: str, arch: str) -> dict:
    """Verify actual binary metadata before recording its identity and digest."""
    control = _binary_control(path)
    actual_source = control.get("Source", control.get("Package", "")).split(" ", 1)[0]
    if actual_source != source or control.get("Version") != version or control.get("Architecture") not in {arch, "all"}:
        raise ValueError(f"Binary metadata mismatch: {path.name}")

    return {"file": path.name, "package": control["Package"], "version": control["Version"],
            "architecture": control["Architecture"], "sha256": sha256(path)}


def _verify_binary_set(directory: Path, metadata: dict[str, str]) -> set[str]:
    """Require every produced deb, and only those debs, to appear in the manifest."""
    referenced = _referenced_debs(metadata)
    if referenced != {path.name for path in directory.glob("*.deb")}:
        raise ValueError("Binary output set differs from .changes")

    return referenced


def _verify_expected_binaries(binaries: list[dict], expected: list[str]) -> None:
    """Reject empty builds and builds missing any expected binary package."""
    missing = sorted(set(expected) - {binary["package"] for binary in binaries})
    if not binaries or missing:
        raise ValueError(f"Expected binaries not produced: {missing or expected}")


def _buildinfo(directory: Path, metadata: dict[str, str], source: str, version: str) -> tuple[Path, dict[str, str]]:
    """Require checksummed build provenance with exact installed dependency versions."""
    paths = [directory / name for _, _, name in checksum_entries(metadata) if name.endswith(".buildinfo")]
    if len(paths) != 1:
        raise ValueError("Expected a checksummed .buildinfo file")

    info = fields(paths[0])
    _verify_identity(info, source, version, "Buildinfo source/version mismatch")
    installed = info.get("Installed-Build-Depends", "")
    versions = dict(re.findall(r"([a-z0-9][a-z0-9+.:\-]*)\s+\(=\s*([^\s)]+)\)", installed))
    if not versions:
        raise ValueError("Buildinfo has no installed build dependency versions")

    return paths[0], versions


def verify_binaries(directory: Path, *, source: str, version: str, expected: list[str], arch: str) -> dict:
    """Verify binary completeness, package identity, and checksummed build provenance."""
    changes, metadata = _binary_changes(directory)
    _verify_identity(metadata, source, version, "Binary .changes source/version mismatch")
    referenced = _verify_binary_set(directory, metadata)
    binaries = [_binary_record(directory / name, source, version, arch) for name in sorted(referenced)]
    _verify_expected_binaries(binaries, expected)
    buildinfo, versions = _buildinfo(directory, metadata, source, version)
    return {"binaries": binaries, "changes": changes.name, "buildinfo": buildinfo.name,
            "build_dependency_versions": versions}
