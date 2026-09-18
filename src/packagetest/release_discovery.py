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
    versions: list[str] = []
    last_repo: str | None = None
    last_hash: str | None = None
    for line in content.splitlines():
        m = _VERSION_RE.match(line)
        if m:
            versions.append(m.group(1).strip())
            continue
        r = _REPO_RE.match(line)
        if r:
            last_repo = r.group(1).strip()
            continue
        h = _HASH_RE.match(line)
        if h:
            last_hash = h.group(1).strip()
    if not versions:
        raise ValueError("No releases found in deliverable YAML")
    return OpenStackRelease(version=versions[-1], project_repo=last_repo, project_hash=last_hash)
