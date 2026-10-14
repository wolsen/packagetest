import json
from pathlib import Path

import pytest

from packagetest.artifacts import fields
from packagetest.failures import FailureBundle, write_failure_bundle
from packagetest.manifest import write_generation_manifest
from packagetest.models import BuildState, CommandResult, GenerationManifest, PackageManifest


def test_deb822_comments_are_ignored_without_ending_a_continuation(tmp_path: Path):
    control = tmp_path / "control"
    control.write_text(
        "Source: python-ldappool\n"
        "Build-Depends: debhelper,\n"
        "               python-all,\n"
        "#               python3-all,\n"
        "               python-setuptools\n"
        "Standards-Version: 3.9.5\n\n"
        "Package: python-ldappool\n",
        encoding="utf-8",
    )

    assert fields(control) == {
        "Source": "python-ldappool",
        "Build-Depends": "debhelper,\npython-all,\npython-setuptools",
        "Standards-Version": "3.9.5",
    }


def test_deb822_rejects_non_comment_lines_without_a_field(tmp_path: Path):
    control = tmp_path / "control"
    control.write_text("Source: example\ninvalid metadata\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid Debian metadata"):
        fields(control)


def test_generation_manifest_written(tmp_path: Path):
    manifest = GenerationManifest(
        generation_id="gen-1",
        openstack_release_target="2027.1-b1",
        ubuntu_release="noble",
        snapshot_at="2026-09-18T05:32:15+00:00",
        package_manifests=[
            PackageManifest(
                source_package="pbr",
                upstream_repo="u",
                upstream_tag_or_sha="tag",
                upstream_version="1.0",
                packaging_repo="p",
                packaging_branch="master",
                packaging_base_sha="abc",
                generated_debian_version="1.0-0ubuntu1",
                source_hashes=["sha256:a"],
                build_dependency_versions={},
                generated_binary_packages=["python3-pbr"],
                generated_binary_hashes=["sha256:b"],
                runner_environment={"os": "ubuntu"},
                build_started_at="now",
                build_finished_at="later",
                build_result=BuildState.PUBLISHED,
            )
        ],
    )
    out = tmp_path / "manifest.json"
    write_generation_manifest(out, manifest)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["generation_id"] == "gen-1"
    assert loaded["snapshot_at"] == "2026-09-18T05:32:15+00:00"
    assert loaded["package_manifests"][0]["build_result"] == "PUBLISHED"


def test_failure_bundle_written(tmp_path: Path):
    result = CommandResult(
        command=["sbuild", "../x.dsc"],
        cwd="/tmp/pkg",
        env_diff={"DEB_BUILD_OPTIONS": "nocheck"},
        stdout="",
        stderr="boom",
        exit_code=1,
        duration_seconds=1.2,
    )
    bundle = FailureBundle(
        category="COMPILATION_FAILURE",
        source_package="glance",
        generation_id="gen-2",
        upstream_sha="123",
        packaging_sha="456",
        failed_command=result.command,
        command_exit_code=result.exit_code,
    )
    out = tmp_path / "fail"
    write_failure_bundle(out_dir=out, bundle=bundle, command_result=result, files={"debian/control": "text"})

    failure_data = json.loads((out / "failure.json").read_text(encoding="utf-8"))
    assert failure_data["category"] == "COMPILATION_FAILURE"
    assert (out / "analysis.md").exists()
    assert (out / "proposed-fix.patch").exists()
