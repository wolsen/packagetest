from __future__ import annotations

import json
from pathlib import Path

from .models import PackageDefinition


def load_package_definitions(path: Path) -> dict[str, PackageDefinition]:
    data = json.loads(path.read_text(encoding="utf-8"))
    definitions: dict[str, PackageDefinition] = {}
    for row in data["packages"]:
        pkg = PackageDefinition(
            source_package=row["source_package"],
            binary_packages=row["binary_packages"],
            upstream_repo=row["upstream_repo"],
            packaging_repo=row["packaging_repo"],
            build_depends_on_sources=row.get("build_depends_on_sources", []),
            branch_mapping=row.get("branch_mapping", {}),
            source_creation_method=row.get("source_creation_method", "opendev-tarball"),
        )
        definitions[pkg.source_package] = pkg
    return definitions
