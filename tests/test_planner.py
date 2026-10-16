from pathlib import Path

from packagetest.config import load_package_definitions
from packagetest.planner import build_plan


def test_plan_topological_order_and_parallel_layer(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        """
{
  "packages": [
    {"source_package":"python-pbr","binary_packages":["python3-pbr"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":[]},
    {"source_package":"python-oslo.i18n","binary_packages":["x"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":["python-pbr"]},
    {"source_package":"python-oslo.serialization","binary_packages":["x"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":["python-pbr"]},
    {"source_package":"glance","binary_packages":["x"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":["python-oslo.i18n","python-oslo.serialization"]}
  ]
}
""",
        encoding="utf-8",
    )
    definitions = load_package_definitions(config)
    plan = build_plan(
        definitions=definitions,
        requested_sources=["glance"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
    )

    order = [b.source_package for b in plan.planned_builds]
    assert order[0] == "python-pbr"
    assert order[-1] == "glance"


def test_plan_can_skip_dependency_closure(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "src" / "packagetest" / "data" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["glance"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
        include_dependency_closure=False,
    )
    assert [b.source_package for b in plan.planned_builds] == ["glance"]
    assert plan.planned_builds[0].depends_on_sources == []


def test_plan_records_snapshot_timestamp(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "src" / "packagetest" / "data" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["python-pbr"],
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at="2026-09-18T05:32:15+00:00",
    )
    assert plan.snapshot_at == "2026-09-18T05:32:15+00:00"


def test_vertical_slice_config_uses_explicit_pbr_packaging_repo():
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "src" / "packagetest" / "data" / "vertical_slice.json")

    assert definitions["python-pbr"].packaging_repo == "https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/python-pbr/+git/python-pbr"
    assert definitions["python-pbr"].packaging_branch is None
