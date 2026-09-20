import json
from argparse import Namespace
from pathlib import Path
from packagetest.cli import plan_cmd, main
from packagetest.release_discovery import ResolvedRelease

def test_plan_cmd_outputs_release_resolution(capsys, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(plan, args):
        return (
            {
                "python-pbr": ResolvedRelease(
                    series="indri",
                    release_id="2027.1",
                    version="5.7.0",
                    project_repo="openstack/pbr",
                    project_hash="abc123",
                    upstream_ref="abc123",
                    snapshot_at="2026-09-18T05:32:15+00:00",
                    deliverable_path="deliverables/_independent/pbr.yaml",
                )
            },
            {},
        )

    monkeypatch.setattr("packagetest.cli._resolve_package_releases", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at="2026-09-18T05:32:15+00:00",
        no_dependency_closure=False,
        sources=["python-pbr"],
    )

    assert plan_cmd(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot_at"] == "2026-09-18T05:32:15+00:00"
    assert payload["planned_builds"][0]["resolved_upstream_version"] == "5.7.0"
    assert payload["planned_builds"][0]["resolved_upstream_tag_or_sha"] == "abc123"


def test_plan_cmd_surfaces_release_resolution_error(capsys, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]

    def fake_resolve(plan, args):
        return ({}, {"python-pbr": "boom"})

    monkeypatch.setattr("packagetest.cli._resolve_package_releases", fake_resolve)
    args = Namespace(
        config=str(repo_root / "config" / "vertical_slice.json"),
        openstack_target="2027.1",
        ubuntu_release="noble",
        snapshot_at=None,
        no_dependency_closure=False,
        sources=["python-pbr"],
    )

    assert plan_cmd(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_builds"][0]["release_resolution_error"] == "boom"



def test_dry_run_is_only_planned(tmp_path, capsys):
    lock = Path(__file__).resolve().parents[1] / 'config/locks/oslo-i18n-noble-baseline.json'
    assert main(['build', '--plan', str(lock), '--run-dir', str(tmp_path), '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['result'] == 'PLANNED'
    assert not list(tmp_path.iterdir())

def test_status_missing(tmp_path):
    assert main(['status', '--manifest', str(tmp_path / 'missing')]) == 1
