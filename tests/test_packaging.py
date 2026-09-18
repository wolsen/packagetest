from pathlib import Path

import pytest

from packagetest.models import PackageDefinition
from packagetest.packaging import build_package_operation_plan, classify_packaging_failure, package_operation_commands


def _package_definition() -> PackageDefinition:
    return PackageDefinition(
        source_package="glance",
        binary_packages=["glance"],
        upstream_repo="https://opendev.org/openstack/glance",
        packaging_repo="https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/glance",
        branch_mapping={"noble": "ubuntu/noble"},
    )


def test_build_package_operation_plan_uses_branch_and_debian_version(tmp_path: Path):
    plan = build_package_operation_plan(
        package=_package_definition(),
        upstream_ref="30.0.0.0rc1",
        upstream_version="30.0.0.0rc1",
        ubuntu_release="noble",
        run_dir=tmp_path,
    )

    assert plan.packaging_branch == "ubuntu/noble"
    assert plan.upstream_version == "30.0.0.0rc1"
    assert plan.generated_debian_version == "30.0.0~rc1-0ubuntu1"
    assert plan.orig_tarball == tmp_path / "glance" / "glance_30.0.0.0rc1.orig.tar.gz"


def test_build_package_operation_plan_requires_branch_mapping(tmp_path: Path):
    with pytest.raises(ValueError, match="No packaging branch configured"):
        build_package_operation_plan(
            package=PackageDefinition(
                source_package="glance",
                binary_packages=["glance"],
                upstream_repo="https://opendev.org/openstack/glance",
                packaging_repo="https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/glance",
            ),
            upstream_ref="30.0.0.0b1",
            upstream_version="30.0.0.0b1",
            ubuntu_release="noble",
            run_dir=tmp_path,
        )


def test_package_operation_commands_cover_milestone_two_steps(tmp_path: Path):
    operation_plan = build_package_operation_plan(
        package=_package_definition(),
        upstream_ref="30.0.0.0b1",
        upstream_version="30.0.0.0b1",
        ubuntu_release="noble",
        run_dir=tmp_path,
    )

    commands = package_operation_commands(
        package=_package_definition(),
        operation_plan=operation_plan,
        ubuntu_release="noble",
    )
    command_texts = [" ".join(command) for command, _ in commands]

    assert any("git clone https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/glance" in text for text in command_texts)
    assert any("checkout -B upstream origin/upstream" in text for text in command_texts)
    assert any("checkout -B pristine-tar origin/pristine-tar" in text for text in command_texts)
    assert any("gbp import-orig --debian-branch=ubuntu/noble --upstream-branch=upstream --pristine-tar --no-interactive --upstream-version=30.0.0.0b1" in text for text in command_texts)
    assert any("gbp pq import" in text for text in command_texts)
    assert any("dch --distribution noble --newversion 30.0.0~b1-0ubuntu1" in text for text in command_texts)


def test_classify_packaging_failure_detects_patch_and_build_failures():
    assert classify_packaging_failure(["bash", "-lc", "check upstream branch"], "", "Missing origin/upstream branch") == "PACKAGING_POLICY_FAILURE"
    assert classify_packaging_failure(["gbp", "pq", "import"], "patch does not apply", "") == "PATCH_APPLY_FAILURE"
    assert classify_packaging_failure(["gbp", "pq", "import"], "fatal: bad revision", "") == "PACKAGING_POLICY_FAILURE"
    assert classify_packaging_failure(["bash", "-lc", "sbuild --dist=noble ../x.dsc"], "", "Unmet build dependency: foo") == "MISSING_BUILD_DEPENDENCY"
    assert classify_packaging_failure(["bash", "-lc", "sbuild --dist=noble ../x.dsc"], "", "compiler error") == "COMPILATION_FAILURE"
