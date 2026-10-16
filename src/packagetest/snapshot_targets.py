"""Derive Ubuntu and UCA snapshot targets from the OpenStack release cadence."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .contracts import BuildTarget


def ubuntu_release_for_openstack(series: str) -> str:
    """Map YYYY.1/.2 to that year's April/October Ubuntu release."""
    match = re.fullmatch(r"(20[0-9]{2})\.([12])", series)
    if not match:
        raise ValueError(f"Invalid OpenStack release: {series}")

    year, release = map(int, match.groups())
    return f"{year % 100:02d}.{'04' if release == 1 else '10'}"


def uca_lts_for_openstack(series: str) -> str:
    """Return the LTS whose next-four-release UCA window contains series.

    An even-year .1 release is the fourth and final UCA on the preceding LTS;
    that same OpenStack release is also seeded directly into the new LTS.
    The new LTS starts its UCA sequence with the even-year .2 release.
    """
    match = re.fullmatch(r"(20[0-9]{2})\.([12])", series)
    if not match:
        raise ValueError(f"Invalid OpenStack release: {series}")

    year, release = map(int, match.groups())
    lts_year = year - 2 if year % 2 == 0 and release == 1 else year - year % 2
    return f"{lts_year % 100:02d}.04"


def _target_pair(series: str, codename: str, suites: dict[str, str]) -> list[dict]:
    ubuntu_release = ubuntu_release_for_openstack(series)
    uca_release = uca_lts_for_openstack(series)
    missing = [release for release in (ubuntu_release, uca_release) if release not in suites]
    if missing:
        raise ValueError(
            f"Ubuntu suite codename mapping is missing: {', '.join(sorted(set(missing)))}")

    ubuntu_suite = suites[ubuntu_release]
    uca_suite = suites[uca_release]
    return [
        {
            "id": f"{codename}-{ubuntu_suite}",
            "label": f"Ubuntu {ubuntu_suite.title()} {ubuntu_release} development",
            "kind": "development",
            "suite": ubuntu_suite,
            "ubuntu_release": ubuntu_release,
            "builder_profile": ubuntu_suite,
            "distribution": ubuntu_suite,
            "base_series": ubuntu_suite,
            "architecture": "amd64",
            "openstack_series": series,
            "openstack_codename": codename,
            "comparison_policy": "target-archive",
            "debian_revision_suffix": f"-0ubuntu1~{codename}1",
            "extra_source_indexes": [],
        },
        {
            "id": f"{codename}-{uca_suite}-uca",
            "label": f"Ubuntu Cloud Archive {codename.title()} for {uca_suite.title()} {uca_release}",
            "kind": "uca",
            "suite": uca_suite,
            "ubuntu_release": uca_release,
            "builder_profile": f"{uca_suite}-uca-{codename}",
            "distribution": f"{uca_suite}-{codename}",
            "base_series": uca_suite,
            "architecture": "amd64",
            "openstack_series": series,
            "openstack_codename": codename,
            "comparison_policy": "target-archive",
            "debian_revision_suffix": "-0ubuntu1~cloud1",
            "cloud_archive": {
                "repository": "http://ubuntu-cloud.archive.canonical.com/ubuntu",
                "distribution": f"{uca_suite}-updates/{codename}",
                "component": "main",
                "keyring": "/usr/share/keyrings/ubuntu-cloud-keyring.gpg",
            },
            "extra_source_indexes": [
                {
                    "filename": f"packagetest-{codename}-updates-{uca_suite}-main-Sources.xz",
                    "url": ("https://ppa.launchpadcontent.net/ubuntu-cloud-archive/"
                            f"{codename}-updates/ubuntu/dists/{uca_suite}/main/source/Sources.xz"),
                    "archive_root": ("https://ppa.launchpadcontent.net/ubuntu-cloud-archive/"
                                     f"{codename}-updates/ubuntu/"),
                    "pocket": f"uca-{codename}-updates",
                    "comparison_priority": 300,
                },
                {
                    "filename": f"packagetest-{codename}-staging-{uca_suite}-main-Sources.xz",
                    "url": ("https://ppa.launchpadcontent.net/ubuntu-cloud-archive/"
                            f"{codename}-staging/ubuntu/dists/{uca_suite}/main/source/Sources.xz"),
                    "archive_root": ("https://ppa.launchpadcontent.net/ubuntu-cloud-archive/"
                                     f"{codename}-staging/ubuntu/"),
                    "pocket": f"uca-{codename}-staging",
                    "comparison_priority": 200,
                },
                {
                    "filename": f"packagetest-{codename}-proposed-{uca_suite}-main-Sources.xz",
                    "url": ("https://ppa.launchpadcontent.net/ubuntu-cloud-archive/"
                            f"{codename}-proposed/ubuntu/dists/{uca_suite}/main/source/Sources.xz"),
                    "archive_root": ("https://ppa.launchpadcontent.net/ubuntu-cloud-archive/"
                                     f"{codename}-proposed/ubuntu/"),
                    "pocket": f"uca-{codename}-proposed",
                    "comparison_priority": 100,
                }
            ],
        },
    ]


def _validate_codename(series: str, codename: str) -> None:
    """Require an OpenStack codename safe for target identifiers."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", codename):
        raise ValueError(f"Invalid OpenStack codename for {series}")


def _validate_mappings(suites: object, releases: object) -> None:
    """Require both mapping tables and at least one OpenStack release."""
    if not isinstance(suites, dict) or not isinstance(releases, dict) or not releases:
        raise ValueError("Snapshot release mappings are incomplete")


def load_targets(path: Path) -> dict[str, dict]:
    """Expand configured releases into distinct distro and UCA targets."""
    config = json.loads(path.read_text())
    suites = config.get("ubuntu_suites")
    releases = config.get("openstack_releases")
    _validate_mappings(suites, releases)
    targets = {}
    for series, codename in releases.items():
        _validate_codename(series, codename)
        for target in _target_pair(series, codename, suites):
            if target["id"] in targets:
                raise ValueError(f'Duplicate snapshot target: {target["id"]}')

            targets[target["id"]] = target

    return targets


def load_target(path: Path, name: str) -> dict:
    try:
        return load_targets(path)[name]
    except KeyError as exc:
        raise ValueError(f"Unknown snapshot target: {name}") from exc


def build_target(target: dict) -> dict:
    result = {
        "id": target["id"],
        "kind": target["kind"],
        "suite": target["suite"],
        "architecture": target["architecture"],
        "backend": "schroot",
        "chroot": f'{target["builder_profile"]}-{target["architecture"]}-sbuild',
        "distribution": target["distribution"],
        "openstack_series": target["openstack_series"],
    }
    if target.get("build_profiles"):
        result["build_profiles"] = target["build_profiles"]

    if target.get("cloud_archive"):
        result["cloud_archive"] = target["cloud_archive"]

    return result


def catalog_target(catalog: dict) -> dict:
    target = catalog.get("target")
    if not isinstance(target, dict):
        raise ValueError("Frozen catalog has no snapshot target")

    required = {"id", "suite", "builder_profile", "distribution", "architecture",
                "openstack_series", "openstack_codename", "debian_revision_suffix"}
    if required - target.keys():
        raise ValueError("Frozen catalog snapshot target is incomplete")

    BuildTarget.from_mapping(target)
    return target
