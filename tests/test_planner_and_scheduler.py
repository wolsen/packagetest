from pathlib import Path
from packagetest.config import load_package_definitions
from packagetest.models import BuildState
from packagetest.planner import build_plan
from packagetest.scheduler import next_ready_packages


def test_plan_topological_order_and_parallel_layer(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        """
{
  "packages": [
    {"source_package":"pbr","binary_packages":["python3-pbr"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":[]},
    {"source_package":"python-oslo.i18n","binary_packages":["x"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":["pbr"]},
    {"source_package":"python-oslo.serialization","binary_packages":["x"],"upstream_repo":"u","packaging_repo":"p","build_depends_on_sources":["pbr"]},
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
    assert order[0] == "pbr"
    assert order[-1] == "glance"


def test_scheduler_blocks_when_dependency_failed(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    definitions = load_package_definitions(repo_root / "config" / "vertical_slice.json")
    plan = build_plan(
        definitions=definitions,
        requested_sources=["glance"],
        openstack_target="2027.1-b1",
        ubuntu_release="noble",
    )
    states = {b.source_package: BuildState.WAITING_FOR_DEPENDENCY for b in plan.planned_builds}
    states["pbr"] = BuildState.BUILD_FAILED

    ready = next_ready_packages(plan, states)
    assert ready == []
    assert states["python-oslo.i18n"] == BuildState.BLOCKED_BY_FAILED_DEPENDENCY
    assert states["python-oslo.serialization"] == BuildState.BLOCKED_BY_FAILED_DEPENDENCY
