"""Versioned build locks and a stage-based, fail-closed packaging executor."""

from __future__ import annotations

import configparser
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .artifacts import checksum_entries, fields, sha256, verify_binaries, verify_source
from .commands import CommandExecutor, CommandRunner
from .contracts import BuildTarget


def _now() -> str:
    """Record a timezone-aware UTC timestamp for build stage provenance."""
    return datetime.now(UTC).isoformat()


def _hex(value: object, length: int) -> bool:
    """Recognize a canonical lowercase hexadecimal identity of the required length."""
    return isinstance(value, str) and bool(
        re.fullmatch(rf"[a-f0-9]{{{length}}}", value)
    )


def needs_lintian_distribution_overlay(target: dict) -> bool:
    """Return whether Lintian needs the target's upload distribution added."""
    return (
        target.get("kind") == "uca"
        or bool(target.get("cloud_archive"))
        or "-uca-" in target.get("profile", "")
        or target["suite"] == "resolute"
    )


def load_lock(path: Path) -> dict[str, Any]:
    """Read and validate a lock before any build workspace is created."""
    lock = json.loads(path.read_text())
    _validate_lock_schema(lock)
    _validate_lock_target(lock)
    seen: set[str] = set()
    for package in lock["packages"]:
        _validate_package_identity(package, seen)
        _validate_package_versions(package)
        _validate_acquisition(package["source"], package["input"])

    # Profile policy compares target and package fields after local validation.
    from .targets import validate_target

    validate_target(lock)
    return lock


def _validate_lock_schema(lock: object) -> None:
    """Validate the outer lock shape before reading its fields."""
    if not isinstance(lock, dict):
        raise ValueError("Build lock must be a JSON object")

    if lock.get("schema_version") != 1:
        raise ValueError("Unsupported lock schema_version (expected 1)")

    if not isinstance(lock.get("target"), dict):
        raise ValueError("Build lock target must be an object")

    _validate_lock_packages(lock.get("packages"))


def _validate_lock_target(lock: dict[str, Any]) -> None:
    """Check target identity and backend constraints."""
    target = lock["target"]
    BuildTarget.from_mapping(target)
    for name in ("suite", "architecture", "chroot"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", target[name]):
            raise ValueError(f"Invalid target {name}")

    _validate_backend(target)


def _validate_package_identity(package: dict[str, Any], seen: set[str]) -> None:
    """Require unique source identities and producers before their consumers."""
    source = package["source"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", source) or source in seen:
        raise ValueError(f"Invalid or duplicate source: {source}")

    for dep in package.get("depends_on", []):
        if dep not in seen:
            raise ValueError(
                f"{source}: dependency {dep} must precede its consumer in lock"
            )

    seen.add(source)


def _validate_package_versions(package: dict[str, Any]) -> None:
    """Keep Debian's version parser authoritative for solver constraints."""
    source = package["source"]
    _validate_required_versions(package)
    if not package.get("expected_binaries"):
        raise ValueError(f"{source}: expected_binaries is required")

    if subprocess.run(
        ["dpkg", "--validate-version", package["version"]], capture_output=True
    ).returncode:
        raise ValueError(f"{source}: invalid Debian version")


def _validate_acquisition(source: str, acquisition: dict[str, Any]) -> None:
    """Dispatch validation according to the explicit source acquisition kind."""
    kind = acquisition["kind"]
    if kind not in {"archive", "gbp", "prepared-snapshot"}:
        raise ValueError("Only archive, gbp and prepared-snapshot inputs are supported")

    if kind == "prepared-snapshot":
        _validate_prepared_pins(acquisition)
        return

    _validate_source_content(source, acquisition)
    if kind == "gbp":
        _validate_gbp(source, acquisition)


def _validate_prepared_pins(acquisition: dict[str, Any]) -> None:
    """Require both the source descriptor checksum and immutable upstream Git pin."""
    if not _hex(acquisition.get("dsc_sha256"), 64) or not _hex(
        acquisition.get("upstream_sha"), 40
    ):
        raise ValueError("Prepared snapshots require source checksum and upstream SHA")


def _validate_download(source: str, item: dict[str, Any]) -> None:
    """Require authenticated transport and a pinned SHA-256 for source downloads."""
    if urlparse(item["url"]).scheme != "https" or not _hex(item["sha256"], 64):
        raise ValueError(f"{source}: an HTTPS URL and SHA256 are required")


def _validate_snapshot(acquisition: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Validate import mode, repository identity, and snapshot artifacts."""
    if (
        acquisition["kind"] != "gbp"
        or acquisition.get("import_mode") != "new"
        or "tarball" in acquisition
    ):
        raise ValueError("Snapshot requires a new gbp import and no release tarball")

    _validate_snapshot_identity(snapshot)
    _validate_snapshot_artifacts(snapshot)


def _validate_build_tool_pins(requirements: list[dict[str, Any]]) -> None:
    """Validate isolated build-tool names, versions, and exact artifact digests."""
    for requirement in requirements:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_.-]+", requirement["name"])
            or not re.fullmatch(r"[0-9][a-zA-Z0-9.+!-]*", requirement["version"])
            or not _hex(requirement["sha256"], 64)
        ):
            raise ValueError("Invalid snapshot build-tool pin")


def _validate_gbp(source: str, acquisition: dict[str, Any]) -> None:
    """Validate Git packaging mode, commit pins, version, and branch names."""
    if acquisition.get("import_mode") not in {"reuse", "new"}:
        raise ValueError("Explicit import_mode reuse or new is required")

    _validate_gbp_commits(source, acquisition)
    if not re.fullmatch(r"[0-9][a-zA-Z0-9.+~\-]*", acquisition["upstream_version"]):
        raise ValueError("Invalid Debian upstream version")

    for key in ("packaging_branch", "upstream_branch", "upstream_tag"):
        if subprocess.run(
            ["git", "check-ref-format", "--branch", acquisition[key]],
            capture_output=True,
        ).returncode:
            raise ValueError(f"Invalid Git ref: {key}")


def _validate_lock_packages(packages: object) -> None:
    """Require a nonempty list of package objects."""
    if not isinstance(packages, list):
        raise ValueError("Build lock packages must be a list")

    if not packages:
        raise ValueError("Empty build lock")

    if not all(isinstance(package, dict) for package in packages):
        raise ValueError("Build lock packages must contain objects")


def _validate_backend(target: dict[str, Any]) -> None:
    """Validate the upload distribution and explicitly selected backend."""
    if "distribution" in target and not re.fullmatch(
        r"[a-z0-9][a-z0-9+.-]*", target["distribution"]
    ):
        raise ValueError("Invalid target distribution")

    if target.get("backend") != "schroot":
        raise ValueError(
            "This slice requires an explicitly provisioned schroot backend"
        )


def _validate_required_versions(package: dict[str, Any]) -> None:
    """Validate dependency names and their exact Debian versions."""
    for binary, version in package.get("required_build_versions", {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9-]+)?", binary):
            raise ValueError("Invalid required binary dependency name")

        if subprocess.run(
            ["dpkg", "--validate-version", version], capture_output=True
        ).returncode:
            raise ValueError("Invalid required binary dependency version")


def _validate_source_content(source: str, acquisition: dict[str, Any]) -> None:
    """Validate either pinned snapshot content or a downloadable source."""
    snapshot = acquisition.get("snapshot")
    if snapshot:
        _validate_snapshot(acquisition, snapshot)
    else:
        item = (
            acquisition["dsc"]
            if acquisition["kind"] == "archive"
            else acquisition["tarball"]
        )
        _validate_download(source, item)


def _validate_snapshot_identity(snapshot: dict[str, Any]) -> None:
    """Require an HTTPS repository, complete Git identities, and a release tag."""
    if urlparse(snapshot["repository"]).scheme != "https":
        raise ValueError("Snapshot repository must use HTTPS")

    for key in ("sha", "base_tag_sha"):
        if not _hex(snapshot[key], 40):
            raise ValueError(f"Snapshot requires a full Git {key}")

    if not re.fullmatch(r"[0-9][a-zA-Z0-9.+~\-]*", snapshot["base_tag"]):
        raise ValueError("Invalid snapshot base tag")


def _validate_snapshot_artifacts(snapshot: dict[str, Any]) -> None:
    """Require supported source serialization and checksum-pinned build tools."""
    if not _hex(snapshot.get("sdist_sha256"), 64):
        raise ValueError("Invalid snapshot sdist checksum")

    if snapshot.get("archive_format", "legacy") not in {"legacy", "portable-v1"}:
        raise ValueError("Unsupported snapshot archive format")

    if not snapshot.get("build_requirements"):
        raise ValueError("Snapshot requires checksum-pinned build tools")

    _validate_build_tool_pins(snapshot["build_requirements"])


def _validate_gbp_commits(source: str, acquisition: dict[str, Any]) -> None:
    """Require full Git commits, including the reused upstream tag."""
    keys = ["packaging_sha", "upstream_sha", "pristine_tar_sha"]
    if acquisition["import_mode"] == "reuse":
        keys.append("upstream_tag_sha")

    for key in keys:
        if not _hex(acquisition[key], 40):
            raise ValueError(f"{source}: pin a full Git SHA for {key}")


class StageFailure(RuntimeError):
    """Signal a failed packaging stage with its captured command diagnostics."""

    pass


class LockedBuild:
    """Own one isolated lock-driven build generation and its persistent evidence."""

    def __init__(
        self,
        lock: dict,
        root: Path,
        *,
        timeout: float = 3600,
        prepared_sources: dict | None = None,
        external_artifacts: dict | None = None,
        runner: CommandExecutor | None = None,
    ):
        """Create generation storage and bind the reviewed lock, producer inputs, and executor."""
        self.lock = lock
        self.prepared_sources = prepared_sources or {}
        self.external_artifacts = external_artifacts or {}
        self.digest = hashlib.sha256(
            json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.root = root.resolve() / f"gen-{self.digest[:12]}-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.root / "logs").mkdir()
        self.runner: CommandExecutor = (
            runner
            if runner is not None
            else CommandRunner(
                self.root / "logs" / "commands.jsonl", stream=True, timeout=timeout
            )
        )
        self.stage = "preflight"
        self.checkout: Path | None = None
        self.last_result = None
        self.manifest = {
            "schema_version": 1,
            "generation_id": self.root.name,
            "lock_sha256": self.digest,
            "target": lock["target"],
            "started_at": _now(),
            "packages": [],
            "reproducibility": "Source inputs pinned; archive dependency versions recorded in buildinfo",
        }
        (self.root / "build-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        self.save()

    def save(self):
        """Atomically replace the generation manifest with the latest recorded build state."""
        path = self.root / "generation-manifest.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2) + "\n")
        temporary.replace(path)

    def command(
        self,
        *argv: str,
        cwd: Path | None = None,
        env: dict | None = None,
        full_output: bool = False,
    ) -> str:
        """Run a staged command, reading full stdout only when explicitly requested."""
        self.last_result = self.runner.run(list(argv), cwd or self.work, env_diff=env)
        if self.last_result.exit_code:
            raise StageFailure(
                f"{self.stage}: {argv[0]} exited {self.last_result.exit_code}: {self.last_result.stderr[-2000:]}"
            )

        if full_output:
            return self.last_result.read_stdout().strip()

        return self.last_result.stdout_tail.strip()

    def enter(self, stage: str):
        """Begin a named stage, reset command evidence, and append its timestamped transition."""
        self.stage = stage
        self.last_result = None
        print(f"[{self.root.name}] {stage}", file=sys.stderr, flush=True)
        with (self.root / "logs" / "stages.jsonl").open("a") as handle:
            handle.write(json.dumps({"stage": stage, "at": _now()}) + "\n")

    def download(self, item: dict, destination: Path):
        """Publish downloaded bytes only after checksum verification and remove partial files."""
        temporary = destination.with_suffix(destination.suffix + ".partial")
        try:
            with (
                urlopen(item["url"], timeout=60) as response,
                temporary.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)

            if sha256(temporary) != item["sha256"]:
                raise StageFailure(f"Checksum mismatch: {item['url']}")

            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def failure(self, source: str, error: Exception) -> None:
        """Publish failure identity and the available packaging diagnostics."""
        directory = self.root / "failures" / source
        directory.mkdir(parents=True, exist_ok=True)
        files = self._failure_files(directory)
        payload = {
            "source_package": source,
            "generation_id": self.root.name,
            "category": self.stage.upper().replace("-", "_"),
            "error": str(error),
            "files": files,
            "lock_sha256": self.digest,
            "command": self.last_result.to_dict() if self.last_result else None,
            "failed_command": self.last_result.command if self.last_result else [],
            "command_exit_code": self.last_result.exit_code if self.last_result else 1,
        }
        (directory / "failure.json").write_text(json.dumps(payload, indent=2) + "\n")
        (directory / "analysis.md").write_text(
            f"# {source}: {self.stage}\n\n{error}\n\nSee failure.json, captured packaging files, and ../../logs/.\n"
        )

    def preflight(self) -> None:
        """Capture tool and builder identity and verify signed target sources."""
        self.enter("environment")
        target = self.lock["target"]
        versions = {}
        for tool in (
            "dpkg",
            "dpkg-source",
            "dpkg-deb",
            "sbuild",
            "git",
            "gbp",
            "schroot",
            "lintian",
        ):
            versions[tool] = self.command(tool, "--version")

        versions["pristine-tar"] = self.command(
            "dpkg-query", "-W", "-f=${Version}", "pristine-tar"
        )
        self.manifest["tools"] = versions
        self.manifest["builder_identity"] = self.command("id")
        self.manifest["kernel"] = self.command("uname", "-srmo")
        chroots = self.command("schroot", "--list").splitlines()
        if not any(
            line.removeprefix("chroot:") == target["chroot"] for line in chroots
        ):
            raise StageFailure(
                f"Missing chroot {target['chroot']}; run scripts/prepare-builder.sh"
            )

        self.command(
            "schroot", "-c", target["chroot"], "--directory", "/", "--", "true"
        )
        self.manifest["apt_sources"] = self.command(
            "schroot",
            "-c",
            target["chroot"],
            "--directory",
            "/",
            "--",
            "sh",
            "-c",
            "cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null; true",
        )
        self._validate_cloud_archive(target)
        self.save()

    def archive_source(self, package: dict, source_dir: Path) -> Path:
        """Download and verify a pinned Debian source set before extracting its checkout."""
        self.enter("source-acquisition")
        item = package["input"]["dsc"]
        filename = Path(urlparse(item["url"]).path).name
        if not filename.endswith(".dsc"):
            raise ValueError("Expected .dsc URL")

        dsc = source_dir / filename
        self.download(item, dsc)
        for digest, _size, name in checksum_entries(fields(dsc)):
            self.download(
                {"url": urljoin(item["url"], name), "sha256": digest}, source_dir / name
            )

        verify_source(dsc, package["source"], package["version"])
        self.checkout = self.work / package["source"]
        self.command("dpkg-source", "--no-check", "-x", str(dsc), str(self.checkout))
        return dsc

    def prepared_source(self, package: dict, source_dir: Path) -> Path:
        """Verify and copy a prepared snapshot descriptor and every referenced component."""
        self.enter("prepared-source-validation")
        dsc = Path(self.prepared_sources[package["source"]])
        if sha256(dsc) != package["input"]["dsc_sha256"]:
            raise StageFailure("Prepared snapshot .dsc checksum mismatch")

        metadata = verify_source(dsc, package["source"], package["version"])
        for name in [dsc.name, *(name for _, _, name in checksum_entries(metadata))]:
            shutil.copy2(dsc.parent / name, source_dir / name)

        return source_dir / dsc.name

    def git_source(self, package: dict, source_dir: Path) -> Path:
        """Prepare a pinned Git packaging source through validated build stages."""
        spec = package["input"]
        checkout = self._checkout_packaging(package)
        self._validate_packaging_config(package, checkout)
        backport = self._validate_packaging_version(package, checkout)
        self.command(
            "git",
            "branch",
            "-f",
            spec["upstream_branch"],
            spec["upstream_sha"],
            cwd=checkout,
        )
        self.command(
            "git",
            "branch",
            "-f",
            "pristine-tar",
            spec["pristine_tar_sha"],
            cwd=checkout,
        )
        tarball = self._acquire_upstream(package, source_dir)
        self._import_upstream(package, checkout, tarball)
        self._validate_quilt(checkout, spec)
        env = self._update_changelog(package, checkout, backport)
        return self._export_git_source(package, checkout, source_dir, env)

    def run(self) -> int:
        """Build packages in dependency order and publish the overall generation verdict."""
        if not self._preflight_successful():
            return 1

        completed = dict(self.external_artifacts)
        failed = set()
        for package in self.lock["packages"]:
            source = package["source"]
            record = {
                "source": source,
                "version": package["version"],
                "started_at": _now(),
                "result": "BUILDING",
            }
            self.manifest["packages"].append(record)
            self.checkout = None
            blocked = self._blocked_dependencies(package, failed)
            if blocked:
                record.update(result="BLOCKED", blocked_by=blocked, finished_at=_now())
                failed.add(source)
                self.save()
                continue

            output = self.root / "artifacts" / source
            source_dir, binary_dir = output / "source", output / "binary"
            source_dir.mkdir(parents=True)
            binary_dir.mkdir()
            self._attempt_package(
                package, record, completed, failed, source_dir, binary_dir
            )
            self.save()

        self.manifest.update(
            result="FAILED" if failed else "SUCCEEDED", finished_at=_now()
        )
        self.save()
        print(
            json.dumps(
                {
                    "result": self.manifest["result"],
                    "run_dir": str(self.root),
                    "lock_sha256": self.digest,
                }
            )
        )
        return int(bool(failed))

    def _failure_files(self, directory: Path) -> dict[str, str]:
        """Collect available packaging files, quilt patches, and Git diagnostics."""
        files = {}
        if self.checkout and self.checkout.exists():
            files = self._packaging_file_contents(self.checkout)
            patches = self.checkout / "debian" / "patches"
            if patches.is_dir():
                shutil.copytree(patches, directory / "patches", dirs_exist_ok=True)

            self._failure_git(directory)

        return files

    def _failure_git(self, directory: Path) -> None:
        """Capture Git status, diff, and recent history when the checkout has a repository."""
        if (self.checkout / ".git").exists():
            for args, name in (
                (["status", "--porcelain=v1"], "git-status.txt"),
                (["diff", "--binary"], "working-tree.patch"),
                (["log", "-5", "--oneline"], "git-log.txt"),
            ):
                result = subprocess.run(
                    ["git", "-C", str(self.checkout), *args],
                    capture_output=True,
                    text=True,
                )
                (directory / name).write_text(result.stdout + result.stderr)

    def _packaging_file_contents(self, checkout: Path) -> dict[str, str]:
        """Read the diagnostic packaging files without requiring every file to exist."""
        files = {}
        for name in (
            "control",
            "rules",
            "changelog",
            "gbp.conf",
            "source/format",
            "source/options",
            "patches/series",
        ):
            path = checkout / "debian" / name
            if path.is_file():
                files["debian/" + name] = path.read_text(errors="replace")

        return files

    def _validate_cloud_archive(self, target: dict) -> None:
        """Require the signed Cloud Archive source and reject trusted overrides."""
        if target.get("cloud_archive"):
            cloud = target["cloud_archive"]
            required = (
                f"deb [signed-by={cloud['keyring']}] {cloud['repository']} "
                f"{cloud['distribution']} {cloud['component']}"
            )
            if (
                required not in self.manifest["apt_sources"]
                or "trusted=yes" in self.manifest["apt_sources"]
            ):
                raise StageFailure(
                    "UCA chroot is missing its signed Cloud Archive repository"
                )

    def _checkout_packaging(self, package: dict) -> Path:
        """Clone the pinned packaging branch and configure deterministic commit identity."""
        self.enter("packaging-checkout")
        spec = package["input"]
        checkout = self.work / package["source"]
        self.checkout = checkout
        self.command("git", "clone", "--no-checkout", spec["repository"], str(checkout))
        self.command(
            "git",
            "checkout",
            "-B",
            spec["packaging_branch"],
            spec["packaging_sha"],
            cwd=checkout,
        )
        self.command(
            "git", "config", "user.name", self.lock["maintainer"]["name"], cwd=checkout
        )
        self.command(
            "git",
            "config",
            "user.email",
            self.lock["maintainer"]["email"],
            cwd=checkout,
        )
        self.command("git", "config", "commit.gpgsign", "false", cwd=checkout)
        self.command("git", "config", "tag.gpgsign", "false", cwd=checkout)
        return checkout

    def _validate_packaging_config(self, package: dict, checkout: Path) -> None:
        """Require gbp branches and the control source to agree with the lock."""
        spec = package["input"]
        conf = configparser.ConfigParser(interpolation=None)
        conf.read(checkout / "debian/gbp.conf")
        configured = conf.defaults()
        if (
            configured.get("debian-branch", spec["packaging_branch"])
            != spec["packaging_branch"]
        ):
            raise StageFailure("Packaging branch conflicts with pinned gbp.conf")

        if configured.get("upstream-branch", "upstream") != spec["upstream_branch"]:
            raise StageFailure("Upstream branch conflicts with pinned gbp.conf")

        if fields(checkout / "debian/control").get("Source") != package["source"]:
            raise StageFailure("debian/control Source differs from lock")

    def _validate_packaging_version(self, package: dict, checkout: Path) -> bool:
        """Check source and upstream identities before selecting the backport version policy."""
        spec = package["input"]
        old_version = self.command("dpkg-parsechangelog", "-S", "Version", cwd=checkout)
        old_source = self.command("dpkg-parsechangelog", "-S", "Source", cwd=checkout)
        if old_source != package["source"]:
            raise StageFailure("Changelog Source differs from lock")

        self._validate_epoch(old_version, package)
        backport = self._backport_policy(package, old_version)
        deb_upstream = package["version"].split(":")[-1].rsplit("-", 1)[0]
        if deb_upstream != spec["upstream_version"]:
            raise StageFailure("Changelog and orig upstream versions differ")

        return backport

    def _validate_epoch(self, old_version: str, package: dict) -> None:
        """Require epoch changes to go through a separate reviewed policy."""
        old_epoch = old_version.split(":")[0] if ":" in old_version else "0"
        new_epoch = (
            package["version"].split(":")[0] if ":" in package["version"] else "0"
        )
        if old_epoch != new_epoch:
            raise StageFailure("Epoch changes require a separate reviewed policy")

    def _backport_policy(self, package: dict, old_version: str) -> bool:
        """Validate the pinned backport base or require a strictly newer Debian version."""
        backport = self.lock["target"].get("profile") == "noble-uca-epoxy"
        if backport:
            if package.get("backport_of") != old_version:
                raise StageFailure(
                    "UCA backport base differs from pinned packaging changelog"
                )
        else:
            self.command(
                "dpkg", "--compare-versions", package["version"], "gt", old_version
            )

        return backport

    def _acquire_upstream(self, package: dict, source_dir: Path) -> Path:
        """Generate a pinned snapshot or download the release tarball before import."""
        spec = package["input"]
        self.enter("upstream-acquisition")
        tarball = (
            source_dir / f"{package['source']}_{spec['upstream_version']}.orig.tar.gz"
        )
        if "snapshot" in spec:
            from .snapshot import build_snapshot

            self.enter("snapshot-sdist")
            self.manifest["packages"][-1]["snapshot"] = build_snapshot(
                self, package, tarball
            )
            self.save()
        else:
            self.download(spec["tarball"], tarball)

        return tarball

    def _import_upstream(self, package: dict, checkout: Path, tarball: Path) -> None:
        """Enforce explicit tag reuse or new import before merging upstream content."""
        spec = package["input"]
        self.enter("upstream-import")
        tag = spec["upstream_tag"]
        existing = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "rev-parse",
                "--verify",
                f"refs/tags/{tag}^{{commit}}",
            ],
            text=True,
            capture_output=True,
        )
        if (existing.returncode == 0) != (spec["import_mode"] == "reuse"):
            raise StageFailure("Tag existence conflicts with explicit import_mode")

        if existing.returncode == 0:
            if existing.stdout.strip() != spec["upstream_tag_sha"]:
                raise StageFailure("Upstream packaging tag has moved")

            pristine = self.work / "pristine" / package["source"]
            pristine.mkdir(parents=True)
            self.command(
                "pristine-tar", "checkout", str(pristine / tarball.name), cwd=checkout
            )
            if sha256(pristine / tarball.name) != spec["tarball"]["sha256"]:
                raise StageFailure(
                    "Existing pristine-tar import differs from pinned release tarball"
                )

            self.command(
                "git",
                "branch",
                "-f",
                spec["upstream_branch"],
                spec["upstream_tag_sha"],
                cwd=checkout,
            )
        else:
            # New imports are deliberately explicit; no overwriting of existing tags.
            self.command(
                "gbp",
                "import-orig",
                "--no-interactive",
                "--no-merge",
                "--pristine-tar",
                f"--debian-branch={spec['packaging_branch']}",
                f"--upstream-branch={spec['upstream_branch']}",
                f"--upstream-tag={tag}",
                f"--upstream-version={spec['upstream_version']}",
                str(tarball),
                cwd=checkout,
            )

        self.command("git", "merge", "--no-edit", tag, cwd=checkout)

    def _validate_quilt(self, checkout: Path, spec: dict) -> None:
        """Check quilt applicability and restore the packaging branch before changelog changes."""
        self.enter("patch-validation")
        series = checkout / "debian/patches/series"
        if self._has_quilt_patches(series):
            self.command("gbp", "pq", "import", "--time-machine=0", cwd=checkout)
            self.command("git", "checkout", spec["packaging_branch"], cwd=checkout)
            self.command("gbp", "pq", "drop", cwd=checkout)

    def _has_quilt_patches(self, series: Path) -> bool:
        """Recognize non-comment quilt entries while allowing an absent series file."""
        if not series.is_file():
            return False

        return any(
            line.strip() and not line.lstrip().startswith("#")
            for line in series.read_text().splitlines()
        )

    def _update_changelog(
        self, package: dict, checkout: Path, backport: bool
    ) -> dict[str, str]:
        """Commit the new upload version and require a clean source-build checkout."""
        spec = package["input"]
        self.enter("changelog")
        env = {
            "DEBFULLNAME": self.lock["maintainer"]["name"],
            "DEBEMAIL": self.lock["maintainer"]["email"],
        }
        self.command(
            "dch",
            *(["--force-bad-version"] if backport else []),
            "--distribution",
            self.lock["target"].get("distribution", self.lock["target"]["suite"]),
            "--force-distribution",
            "--newversion",
            package["version"],
            f"Build pinned upstream release {spec['upstream_version']} for packaging validation.",
            cwd=checkout,
            env=env,
        )
        self.command("git", "add", "debian/changelog", cwd=checkout)
        self.command(
            "git",
            "commit",
            "-m",
            f"New upstream release {spec['upstream_version']}",
            cwd=checkout,
        )
        if self.command("git", "status", "--porcelain", cwd=checkout):
            raise StageFailure("Packaging checkout is not clean before source build")

        return env

    def _export_git_source(
        self, package: dict, checkout: Path, source_dir: Path, env: dict[str, str]
    ) -> Path:
        """Build and verify one source bundle, preserving packaging diff evidence."""
        spec = package["input"]
        self.enter("source-build")
        export_dir = self.work / "export" / package["source"]
        export_dir.mkdir(parents=True)
        self.command(
            "gbp",
            "buildpackage",
            f"--git-debian-branch={spec['packaging_branch']}",
            f"--git-upstream-branch={spec['upstream_branch']}",
            "--git-pristine-tar",
            f"--git-upstream-tag={spec['upstream_tag']}",
            f"--git-export-dir={export_dir}",
            "--git-builder=dpkg-buildpackage -S -d -nc -us -uc",
            cwd=checkout,
            env=env,
        )
        candidates = list(export_dir.glob("*.dsc"))
        if len(candidates) != 1:
            raise StageFailure(
                f"Expected one source .dsc in {export_dir}, found {len(candidates)}"
            )

        dsc = candidates[0]
        verify_source(dsc, package["source"], package["version"])
        for pattern in (
            "*.dsc",
            "*.orig.tar.*",
            "*.debian.tar.*",
            "*.changes",
            "*.buildinfo",
        ):
            for path in export_dir.glob(pattern):
                shutil.copy2(path, source_dir / path.name)

        self.manifest["packages"][-1]["packaging_result_sha"] = self.command(
            "git", "rev-parse", "HEAD", cwd=checkout
        )
        self.command("git", "diff", spec["packaging_sha"], "--", "debian", cwd=checkout)
        patch = subprocess.check_output(
            ["git", "-C", str(checkout), "diff", spec["packaging_sha"], "--", "debian"]
        )
        (source_dir.parent / "packaging.patch").write_bytes(patch)
        return source_dir / dsc.name

    def _build_package(
        self,
        package: dict,
        record: dict,
        completed: dict,
        source_dir: Path,
        binary_dir: Path,
    ) -> None:
        """Acquire source, build binaries, and validate dependency and lintian evidence."""
        source = package["source"]
        kind = package["input"]["kind"]
        acquire = {
            "archive": self.archive_source,
            "gbp": self.git_source,
            "prepared-snapshot": self.prepared_source,
        }[kind]
        dsc = acquire(package, source_dir)
        record["source_artifacts"] = [
            {"file": path.name, "sha256": sha256(path)}
            for path in sorted(source_dir.iterdir())
            if path.is_file()
        ]
        self._build_binaries(package, record, completed, dsc, binary_dir)
        validation = self._validate_binaries(package, binary_dir)
        self._run_lintian(record, binary_dir, validation)
        record.update(validation, result="SUCCEEDED", finished_at=_now())
        completed[source] = [
            {**b, "path": binary_dir / b["file"]} for b in validation["binaries"]
        ]

    def _dependency_arguments(
        self, package: dict, completed: dict, record: dict, argv: list[str]
    ) -> None:
        """Verify producer artifact bytes before passing them into sbuild."""
        for dep in package.get("depends_on", []) + package.get(
            "external_dependencies", []
        ):
            for artifact in completed[dep]:
                path = Path(artifact["path"])
                if sha256(path) != artifact["sha256"]:
                    raise StageFailure(
                        f"Dependency artifact changed after validation: {path}"
                    )

                argv.append(f"--extra-package={path}")
                record["dependency_artifacts"].append(
                    {**artifact, "path": str(path), "source": dep}
                )

    def _build_binaries(
        self, package: dict, record: dict, completed: dict, dsc: Path, binary_dir: Path
    ) -> None:
        """Run sbuild with verified producers, exact dependency versions, and build profiles."""
        self.enter("binary-build")
        target = self.lock["target"]
        argv = [
            "sbuild",
            "--verbose",
            f"--chroot-mode={target['backend']}",
            f"--chroot={target['chroot']}",
            f"--dist={target.get('distribution', target['suite'])}",
            f"--arch={target['architecture']}",
            "--arch-all",
            f"--build-dir={binary_dir}",
            "--no-run-lintian",
        ]
        record["dependency_artifacts"] = []
        self._dependency_arguments(package, completed, record, argv)
        for binary, version in package.get("required_build_versions", {}).items():
            argv.append(f"--add-depends={binary} (= {version})")

        self.save()
        if target.get("build_profiles"):
            argv.append("--profiles=" + ",".join(target["build_profiles"]))

        self.command(*argv, str(dsc), cwd=binary_dir)

    def _validate_binaries(self, package: dict, binary_dir: Path) -> dict:
        """Verify binary identities, upload distribution, and installed dependency versions."""
        source, target = package["source"], self.lock["target"]
        self.enter("artifact-validation")
        validation = verify_binaries(
            binary_dir,
            source=source,
            version=package["version"],
            expected=package["expected_binaries"],
            arch=target["architecture"],
        )
        distribution = target.get("distribution", target["suite"])
        if (
            fields(binary_dir / validation["changes"]).get("Distribution")
            != distribution
        ):
            raise StageFailure("Binary .changes distribution differs from target")

        for dep, version in package.get("required_build_versions", {}).items():
            if validation["build_dependency_versions"].get(dep) != version:
                raise StageFailure(
                    f"Build did not use required dependency {dep}={version}"
                )

        return validation

    def _run_lintian(self, record: dict, binary_dir: Path, validation: dict) -> None:
        """Run all lintian checks with target distribution metadata when needed."""
        target = self.lock["target"]
        distribution = target.get("distribution", target["suite"])
        self.enter("lintian")
        lintian_args = []
        if needs_lintian_distribution_overlay(target):
            # Host Lintian knows Ubuntu suites but not every UCA upload
            # distribution. Extend its distribution data; disable no checks.
            overlay = self.root / "lintian"
            data = overlay / "vendors/ubuntu/main/data/changes-file/known-dists"
            data.parent.mkdir(parents=True, exist_ok=True)
            installed = Path(
                "/usr/share/lintian/vendors/ubuntu/main/data/changes-file/known-dists"
            )
            data.write_text(installed.read_text() + "\n" + distribution + "\n")
            record["lintian_distribution_data_sha256"] = sha256(data)
            lintian_args = ["--include-dir", str(overlay), "--profile", "ubuntu"]

        self.command(
            "lintian",
            *lintian_args,
            "--fail-on=error",
            str(binary_dir / validation["changes"]),
            cwd=binary_dir,
        )

    def _preflight_successful(self) -> bool:
        """Publish an environment failure without attempting any package builds."""
        try:
            self.preflight()
        except Exception as exc:
            self.failure("environment", exc)
            self.manifest.update(result="FAILED", error=str(exc), finished_at=_now())
            self.save()
            return False

        return True

    def _attempt_package(
        self,
        package: dict,
        record: dict,
        completed: dict,
        failed: set[str],
        source_dir: Path,
        binary_dir: Path,
    ) -> None:
        """Record a package failure while allowing independent packages to continue."""
        source = package["source"]
        try:
            self._build_package(package, record, completed, source_dir, binary_dir)
        except Exception as exc:
            failed.add(source)
            self.failure(source, exc)
            record.update(
                result="FAILED",
                failed_stage=self.stage,
                error=str(exc),
                finished_at=_now(),
            )

    def _blocked_dependencies(self, package: dict, failed: set[str]) -> list[str]:
        """Preserve dependency order when reporting upstream build failures."""
        return [dep for dep in package.get("depends_on", []) if dep in failed]
