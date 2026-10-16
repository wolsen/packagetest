"""Load validated package definitions and locate bundled configuration defaults."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import object_mapping, records, required_text, string_list, text_field
from .models import PackageDefinition


def load_package_definitions(path: Path) -> dict[str, PackageDefinition]:
    """Validate package configuration fields before constructing build definitions."""
    data = object_mapping(
        json.loads(path.read_text(encoding="utf-8")), "package configuration"
    )
    definitions: dict[str, PackageDefinition] = {}
    for row in records(data.get("packages"), "packages"):
        pkg = PackageDefinition(
            source_package=required_text(row, "source_package"),
            binary_packages=string_list(row.get("binary_packages"), "binary_packages"),
            upstream_repo=required_text(row, "upstream_repo"),
            packaging_repo=required_text(row, "packaging_repo"),
            build_depends_on_sources=string_list(
                row.get("build_depends_on_sources", []), "build_depends_on_sources"
            ),
            packaging_branch=_optional_text(row, "packaging_branch"),
            source_creation_method=text_field(
                row, "source_creation_method", "opendev-tarball"
            ),
            openstack_deliverable=_optional_text(row, "openstack_deliverable"),
        )
        definitions[pkg.source_package] = pkg
    return definitions


def _optional_text(row: dict[str, object], key: str) -> str | None:
    """Preserve nullable optional configuration strings without coercion."""
    return None if row.get(key) is None else text_field(row, key)


def default_config_path(name: str) -> Path:
    """Locate shipped defaults in both an editable checkout and an installed wheel."""
    if name not in {"vertical_slice.json", "hibiscus-candidate-dependencies.json"}:
        raise ValueError(f"Unknown bundled configuration: {name}")
    return Path(__file__).with_name("data") / name
