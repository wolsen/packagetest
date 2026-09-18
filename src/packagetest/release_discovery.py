from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenStackRelease:
    version: str
    project_repo: str | None
    project_hash: str | None


_VERSION_RE = re.compile(r"^\s*-\s+version:\s+(.+?)\s*$")
_REPO_RE = re.compile(r"^\s*-\s+repo:\s+(.+?)\s*$")
_HASH_RE = re.compile(r"^\s+hash:\s+([0-9a-f]{7,40})\s*$")


def latest_release_from_deliverable_yaml(content: str) -> OpenStackRelease:
    releases: list[OpenStackRelease] = []
    current: OpenStackRelease | None = None
    for line in content.splitlines():
        m = _VERSION_RE.match(line)
        if m:
            if current is not None:
                releases.append(current)
            current = OpenStackRelease(version=m.group(1).strip(), project_repo=None, project_hash=None)
            continue
        r = _REPO_RE.match(line)
        if r and current is not None:
            current = OpenStackRelease(
                version=current.version,
                project_repo=r.group(1).strip(),
                project_hash=current.project_hash,
            )
            continue
        h = _HASH_RE.match(line)
        if h and current is not None:
            current = OpenStackRelease(
                version=current.version,
                project_repo=current.project_repo,
                project_hash=h.group(1).strip(),
            )
    if current is not None:
        releases.append(current)
    if not releases:
        raise ValueError("No releases found in deliverable YAML")
    return releases[-1]
