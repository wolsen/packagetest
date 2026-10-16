#!/usr/bin/env python3
"""Download matching same-run artifacts through the complete paginated REST inventory."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

PAGE_SIZE = 100


class ArtifactRedirectHandler(HTTPRedirectHandler):
    """Keep the GitHub token off redirected blob-storage requests."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        """Let urllib validate the redirect, then remove API-only authorization."""
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")

        return redirected


def matches(name: str, pattern: str) -> bool:
    """Match a glob or the flat brace list emitted by the dependency planner."""
    if pattern.startswith("{") and pattern.endswith("}"):
        return any(fnmatch.fnmatchcase(name, part) for part in pattern[1:-1].split(","))

    return fnmatch.fnmatchcase(name, pattern)


def safe_artifact_name(name: str) -> str:
    """Require one filesystem component before creating an artifact directory."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name):
        raise ValueError(f"Unsafe artifact name: {name!r}")

    return name


def validate_selection(selected: list[dict[str, Any]]) -> None:
    """Require unique artifact destinations and reject expired matches."""
    names = [safe_artifact_name(item["name"]) for item in selected]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate matching artifact names in this workflow run")

    if any(item["expired"] for item in selected):
        raise ValueError("Matching workflow artifacts have expired")


def verify_archive(path: Path, artifact: dict[str, Any]) -> None:
    """Check the service-provided SHA-256 digest when the artifact exposes one."""
    digest = artifact.get("digest")
    if digest is None:
        return

    with path.open("rb") as stream:
        actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != actual:
        raise ValueError(f"Artifact checksum mismatch: {artifact['name']}")


def validate_zip_member(member: zipfile.ZipInfo) -> None:
    """Reject traversal, absolute paths, and links before extracting artifact bytes."""
    path = PurePosixPath(member.filename)
    if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
        raise ValueError(f"Unsafe artifact member: {member.filename!r}")

    if stat.S_ISLNK(member.external_attr >> 16):
        raise ValueError(f"Symlink in artifact: {member.filename!r}")


def extract_archive(path: Path, destination: Path) -> None:
    """Validate the complete ZIP member inventory before extracting into fresh staging."""
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            validate_zip_member(member)

        archive.extractall(destination)


def publish_archive(staging: Path, destination: Path, name: str, merge: bool) -> None:
    """Publish one artifact directory or merge unique top-level producer bundle files."""
    if not merge:
        staging.rename(destination / name)
        return

    for path in staging.iterdir():
        target = destination / path.name
        if target.exists() or target.is_symlink():
            raise ValueError(f"Conflicting artifact member: {path.name}")

        shutil.move(str(path), target)


class WorkflowArtifacts:
    """Own one run's API inventory and authenticated downloads without a 1,000-item cap."""

    def __init__(
        self,
        repository: str,
        run_id: str,
        token: str,
        opener: OpenerDirector | None = None,
    ) -> None:
        """Bind a repository and run while keeping credentials inside API requests."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected repository in owner/name form")

        if not run_id.isdigit():
            raise ValueError("Expected numeric workflow run ID")

        self.base = f"https://api.github.com/repos/{repository}"
        self.run_id = run_id
        self.token = token
        self.opener = (
            opener if opener is not None else build_opener(ArtifactRedirectHandler())
        )

    def _request(self, url: str) -> Request:
        """Authenticate only the initial GitHub REST request, never its blob redirect."""
        return Request(
            url,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _record_page(
        self, artifacts: dict[int, dict[str, Any]], values: list[dict[str, Any]]
    ) -> None:
        """Deduplicate repeated IDs while rejecting contradictory artifact identities."""
        for artifact in values:
            identity = int(artifact["id"])
            previous = artifacts.get(identity)
            if previous is not None and (previous["name"], previous.get("digest")) != (
                artifact["name"],
                artifact.get("digest"),
            ):
                raise ValueError(
                    f"Artifact identity changed during inventory: {identity}"
                )

            artifacts.setdefault(identity, artifact)

    def inventory(self) -> list[dict[str, Any]]:
        """Read all pages, coalescing artifacts repeated by a changing live inventory."""
        artifacts: dict[int, dict[str, Any]] = {}
        page = 1
        while True:
            url = f"{self.base}/actions/runs/{self.run_id}/artifacts?per_page={PAGE_SIZE}&page={page}"
            with self.opener.open(self._request(url), timeout=60) as response:
                values = json.load(response)["artifacts"]
            # Concurrent uploads can shift offset-based page boundaries. The
            # same ID may reappear; distinct IDs with one name remain ambiguous.
            self._record_page(artifacts, values)
            if len(values) < PAGE_SIZE:
                return list(artifacts.values())

            page += 1

    def _selected(self, pattern: str, allow_empty: bool) -> list[dict[str, Any]]:
        """Reject ambiguous or expired matches instead of silently losing source statuses."""
        selected = [item for item in self.inventory() if matches(item["name"], pattern)]
        validate_selection(selected)
        if not selected and not allow_empty:
            raise ValueError(f"No workflow artifacts match {pattern!r}")

        return selected

    def _download(
        self, artifact: dict[str, Any], destination: Path, merge: bool
    ) -> None:
        """Verify and stage one ZIP before exposing its files to workflow consumers."""
        with tempfile.TemporaryDirectory(
            prefix=".artifact-", dir=destination
        ) as temporary:
            root = Path(temporary)
            archive = root / "artifact.zip"
            url = f"{self.base}/actions/artifacts/{int(artifact['id'])}/zip"
            with (
                self.opener.open(self._request(url), timeout=120) as response,
                archive.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)

            verify_archive(archive, artifact)
            staging = root / "contents"
            staging.mkdir()
            extract_archive(archive, staging)
            publish_archive(staging, destination, artifact["name"], merge)

    def download(
        self,
        pattern: str,
        destination: Path,
        *,
        merge: bool = False,
        allow_empty: bool = False,
    ) -> int:
        """Download all matching artifacts, failing loudly on incomplete retrieval."""
        selected = self._selected(pattern, allow_empty)
        destination.mkdir(parents=True, exist_ok=True)
        for artifact in selected:
            self._download(artifact, destination, merge)

        print(json.dumps({"downloaded_artifacts": len(selected), "pattern": pattern}))
        return len(selected)


def main() -> None:
    """Download selected artifacts from this run using the workflow's read-only token."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--merge-multiple", action="store_true")
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    client = WorkflowArtifacts(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_RUN_ID"],
        os.environ["GH_TOKEN"],
    )
    client.download(
        args.pattern,
        args.output,
        merge=args.merge_multiple,
        allow_empty=args.allow_empty,
    )


if __name__ == "__main__":
    main()
