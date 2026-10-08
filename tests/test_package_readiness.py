import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('build,test,expected', [
    ('SUCCEEDED', 'PASS', 'READY'),
    ('SUCCEEDED', 'SUPERFICIAL', 'READY'),
    ('SUCCEEDED', 'SKIP', 'READY'),
    ('SUCCEEDED', 'NO_TESTS', 'READY'),
    ('SUCCEEDED', 'FAIL', 'FAILED'),
    ('FAILED', 'PASS', 'FAILED'),
])
def test_package_readiness_binds_build_and_test_results(tmp_path, build, test, expected):
    outputs = tmp_path / 'outputs'
    generation = outputs / 'gen-123'
    generation.mkdir(parents=True)
    (generation / 'generation-manifest.json').write_text('{}\n')
    build_path = tmp_path / 'build.json'
    test_path = tmp_path / 'test.json'
    build_path.write_text(json.dumps({'result': build}))
    test_path.write_text(json.dumps({'result': test}))

    subprocess.run([
        'python3', 'scripts/package-readiness.py', '--source', 'sample',
        '--outputs', str(outputs), '--build', str(build_path),
        '--autopkgtest', str(test_path), '--run-id', '123', '--run-attempt', '2',
    ], check=True, cwd=Path(__file__).parents[1], env=os.environ.copy())

    report = json.loads((generation / 'producer-readiness.json').read_text())
    assert report == {
        'schema_version': 1, 'source': 'sample', 'result': expected,
        'build_result': build, 'autopkgtest_result': test,
        'ci': {'run_id': '123', 'run_attempt': '2'},
    }


def test_package_readiness_records_missing_candidate_without_masking_infra_failure(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    build_path = tmp_path / 'build.json'
    test_path = tmp_path / 'test.json'
    build_path.write_text(json.dumps({'result': 'INFRA_ERROR'}))
    test_path.write_text(json.dumps({'result': 'BLOCKED'}))

    completed = subprocess.run([
        'python3', 'scripts/package-readiness.py', '--source', 'sample',
        '--outputs', str(outputs), '--build', str(build_path),
        '--autopkgtest', str(test_path), '--run-id', '123', '--run-attempt', '2',
    ], check=True, cwd=Path(__file__).parents[1], env=os.environ.copy(),
       text=True, capture_output=True)

    report = json.loads((outputs / 'producer-readiness.json').read_text())
    assert report == {
        'schema_version': 1, 'source': 'sample', 'result': 'FAILED',
        'build_result': 'INFRA_ERROR', 'autopkgtest_result': 'BLOCKED',
        'ci': {'run_id': '123', 'run_attempt': '2'},
        'error': 'No generation manifest was produced',
    }
    assert json.loads(completed.stdout) == report
