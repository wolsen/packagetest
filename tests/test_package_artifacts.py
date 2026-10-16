"""Verify publication policy, evidence retention, and consolidated report consumption."""
import hashlib
import json
import runpy
import tarfile
from pathlib import Path

import pytest

from packagetest.nightly_plan import _job_result_rows
from packagetest.pipeline_report import load_proposals

module = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/publish-package-artifacts.py'))
PackageArtifacts = module['PackageArtifacts']


def package_inputs(tmp_path: Path, build: str, test: str, ready: bool) -> tuple[Path, Path, Path]:
    """Create final verdicts, compact proposals, and representative bulky evidence."""
    outputs = tmp_path / 'outputs'
    generation = outputs / 'gen-sample'
    artifacts = generation / 'artifacts/sample/binary'
    artifacts.mkdir(parents=True)
    (artifacts / 'sample.deb').write_bytes(b'candidate binary')
    (generation / 'generation-manifest.json').write_text(json.dumps({'result': build}))
    (generation / 'build-lock.json').write_text('{}')
    (generation / 'producer-readiness.json').write_text(json.dumps({
        'source': 'sample', 'result': 'READY' if ready else 'FAILED',
        'build_result': build, 'autopkgtest_result': test}))
    (outputs / 'result.json').write_text(json.dumps({'source': 'sample', 'result': build}))
    (outputs / 'build.log').write_text('complete build log\n')
    (generation / 'work').mkdir()
    (generation / 'work/disposable').write_text('temporary')
    proposal = outputs / 'source-preparation/packaging-proposal'
    proposal.mkdir(parents=True)
    patch = b'diff --git a/debian/control b/debian/control\n'
    (proposal / 'proposal.patch').write_bytes(patch)
    (proposal / 'packaging-proposal.json').write_text(json.dumps({
        'source': 'sample', 'actions': [],
        'patch': {'file': 'proposal.patch', 'sha256': hashlib.sha256(patch).hexdigest()}}))
    recovered = outputs / 'source-preparation/remediation-source'
    recovered.mkdir()
    (recovered / 'module.py').write_text('duplicate baseline')
    tests = tmp_path / 'test-results'
    (tests / 'result').mkdir(parents=True)
    (tests / 'result/result.json').write_text(json.dumps({'source': 'sample', 'result': test}))
    (tests / 'console.log').write_text('complete test log\n')
    (tests / 'disk.qcow2').write_bytes(b'VM disk')
    ai = tmp_path / 'ai-remediation/sample'
    (ai / 'attempt-1').mkdir(parents=True)
    (ai / 'attempt-1/model-output.txt').write_text('complete model output')
    (ai / 'attempt-1/proposal.patch').write_text('accepted cumulative repair')
    (ai / 'result.json').write_text(json.dumps({'source': 'sample', 'selected_attempt': 1 if ready else None}))
    (ai / 'summary.md').write_text('repair summary')
    return outputs, tests, ai


@pytest.mark.parametrize('build, test, ready, diagnostics', [
    ('SUCCEEDED', 'PASS', True, False),
    ('SUCCEEDED', 'SUPERFICIAL', True, False),
    ('SUCCEEDED', 'NO_TESTS', True, False),
    ('SUCCEEDED', 'SKIP', True, False),
    ('FAILED', 'BLOCKED', False, True),
    ('SUCCEEDED', 'FAIL', False, True),
    ('INFRA_ERROR', 'BLOCKED', False, True),
    ('BLOCKED', 'BLOCKED', False, False),
])
def test_publication_keeps_reports_and_conditions_large_payloads(
        tmp_path: Path, build: str, test: str, ready: bool, diagnostics: bool) -> None:
    """Every package gets a report; only accepted producers and real failures get archives."""
    outputs, tests, ai = package_inputs(tmp_path, build, test, ready)
    publication = tmp_path / 'publication'
    flags = PackageArtifacts('sample', outputs, tests, ai, publication).publish()

    assert flags == {'producer': ready, 'diagnostics': diagnostics}
    assert (publication / 'report/build.json').read_bytes() == (outputs / 'result.json').read_bytes()
    assert (publication / 'report/autopkgtest.json').read_bytes() == (tests / 'result/result.json').read_bytes()
    assert (publication / 'producer/sample.tar.gz').exists() == ready
    assert (publication / 'diagnostics/sample.tar.gz').exists() == diagnostics
    assert not (publication / 'report/remediation/attempt-1').exists()
    assert (outputs / 'build.log').read_text() == 'complete build log\n'
    if ready:
        assert (publication / 'report/remediation/accepted-repair.patch').read_text() == 'accepted cumulative repair'
        with tarfile.open(publication / 'producer/sample.tar.gz') as archive:
            assert 'gen-sample/generation-manifest.json' in archive.getnames()
            assert not any('work' in name or 'build.log' in name for name in archive.getnames())

    if diagnostics:
        with tarfile.open(publication / 'diagnostics/sample.tar.gz') as archive:
            names = archive.getnames()
            assert 'outputs/build.log' in names
            assert 'test-results/console.log' in names
            assert 'ai-remediation/attempt-1/model-output.txt' in names
            assert not any('remediation-source' in name or 'disposable' in name or '.qcow2' in name for name in names)


def test_combined_report_supplies_both_verdicts_and_verified_proposals(tmp_path: Path) -> None:
    """The summary reads one download without confusing nested repair result files."""
    outputs, tests, ai = package_inputs(tmp_path, 'SUCCEEDED', 'PASS', True)
    publication = tmp_path / 'publication'
    PackageArtifacts('sample', outputs, tests, ai, publication).publish()
    reports = tmp_path / 'reports'
    reports.mkdir()
    (publication / 'report').rename(reports / 'package-report-target-sample')

    rows = _job_result_rows(['sample', 'missing'], 'target-', reports)
    assert rows[0]['build']['result'] == 'SUCCEEDED'
    assert rows[0]['autopkgtest']['result'] == 'PASS'
    assert rows[1]['build']['result'] == 'MISSING'
    proposals, warnings = load_proposals(reports, {'sample'}, tmp_path / 'summary')
    assert warnings == []
    assert proposals['sample']['patch']['download'] == 'patches/sample.patch'


def test_missing_readiness_cannot_publish_a_successful_build_as_a_producer(tmp_path: Path) -> None:
    """Build/test records alone cannot bypass the readiness publisher's decision."""
    outputs, tests, ai = package_inputs(tmp_path, 'SUCCEEDED', 'PASS', True)
    (outputs / 'gen-sample/producer-readiness.json').unlink()
    flags = PackageArtifacts('sample', outputs, tests, ai, tmp_path / 'publication').publish()
    assert flags == {'producer': False, 'diagnostics': True}


def test_publication_cli_appends_boolean_workflow_outputs(tmp_path: Path) -> None:
    """The composite action receives flags without replacing earlier step outputs."""
    import subprocess
    import sys

    outputs, tests, ai = package_inputs(tmp_path, 'SUCCEEDED', 'PASS', True)
    github_output = tmp_path / 'github-output'
    github_output.write_text('existing=value\n')
    subprocess.run([sys.executable, 'scripts/publish-package-artifacts.py', '--source', 'sample',
                    '--outputs', str(outputs), '--tests', str(tests), '--remediation', str(ai),
                    '--output', str(tmp_path / 'publication'), '--github-output', str(github_output)],
                   check=True, capture_output=True, text=True)
    assert github_output.read_text() == 'existing=value\nproducer=true\ndiagnostics=false\n'
