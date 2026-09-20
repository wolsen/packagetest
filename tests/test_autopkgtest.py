import json
from pathlib import Path

import pytest

from packagetest.autopkgtest import checked_file, classify, exit_status, run


@pytest.mark.parametrize('code,summary,result', [
    (0, 'smoke PASS\n', 'PASS'),
    (0, '', 'INFRA_ERROR'),
    (0, 'smoke SKIP not supported\n', 'SKIP'),
    (2, 'smoke PASS\nmysql SKIP unavailable\n', 'SKIP'),
    (2, 'smoke FLAKY transient\n', 'SKIP'),
    (4, 'smoke FAIL bad response\n', 'FAIL'),
    (6, 'smoke FAIL bad response\nmysql SKIP unavailable\n', 'FAIL'),
    (8, '* SKIP no tests in this package\n', 'NO_TESTS'),
    (8, 'smoke SKIP isolation-machine required\n', 'SKIP'),
    (8, 'autodep8-python3 PASS (superficial)\n', 'SUPERFICIAL'),
    (8, 'autodep8-python3 PASS (superficial)\nmysql SKIP unavailable\n', 'SKIP'),
    (0, 'autodep8-python3 PASS (superficial)\n', 'SUPERFICIAL'),
    (0, 'autodep8-python3 PASS (superficial)\nsmoke PASS\n', 'PASS'),
    (12, '', 'FAIL'),
    (14, '', 'FAIL'),
    (16, '', 'INFRA_ERROR'),
    (20, '', 'INFRA_ERROR'),
    (-9, '', 'INFRA_ERROR'),
])
def test_result_categories(code, summary, result):
    assert classify(code, summary)['result'] == result


def test_summary_preserves_skip_reason():
    report = classify(2, 'mysql SKIP isolation-machine required\n')
    assert report['tests'] == [{'name': 'mysql', 'result': 'SKIP', 'detail': 'isolation-machine required'}]


def test_rejects_corrupt_or_traversing_artifact(tmp_path):
    (tmp_path / 'test.deb').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='corrupt'):
        checked_file(tmp_path, {'file': 'test.deb', 'sha256': '0' * 64})
    with pytest.raises(ValueError, match='basename'):
        checked_file(tmp_path, {'file': '../test.deb', 'sha256': '0' * 64})


def test_infrastructure_failure_produces_report(tmp_path):
    report = run(tmp_path / 'missing.json', 'glance', tmp_path / 'result', backend='qemu', image='missing.img')
    assert report['result'] == 'INFRA_ERROR'
    assert json.loads((tmp_path / 'result/result.json').read_text()) == report


def test_existing_output_is_not_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        run(tmp_path / 'missing.json', 'glance', tmp_path, backend='qemu', image='missing.img')


@pytest.mark.parametrize('ci,result', [
    ({'run_id': '123', 'run_attempt': '1'}, 'BLOCKED'),
    ({'run_id': '122', 'run_attempt': '1'}, 'INFRA_ERROR'),
    ({'run_id': '123', 'run_attempt': '2'}, 'INFRA_ERROR'),
])
def test_nightly_rejects_unbuilt_and_wrong_run_inputs(tmp_path, ci, result):
    import os
    import subprocess
    import sys
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'packages': [{'source': 'glance'}]}))
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    (inputs / 'generation-manifest.json').write_text(json.dumps({
        'ci': ci, 'result': 'FAILED', 'packages': [{'source': 'glance', 'result': 'FAILED'}]}))
    output = tmp_path / 'result'
    completed = subprocess.run([
        sys.executable, 'scripts/nightly-autopkgtest.py', '--catalog', str(catalog),
        '--source', 'glance', '--inputs', str(inputs), '--output', str(output),
        '--run-id', '123', '--run-attempt', '1', '--image', '/nonexistent.img',
    ], env={**os.environ, 'PYTHONPATH': str(Path('src').resolve())}, capture_output=True, text=True)
    assert completed.returncode == 1
    assert json.loads((output / 'result.json').read_text())['result'] == result


@pytest.mark.parametrize('result,expected', [
    ('PASS', 0), ('SUPERFICIAL', 2), ('SKIP', 2), ('NO_TESTS', 2),
    ('FAIL', 1), ('INFRA_ERROR', 1), ('BLOCKED', 1),
])
def test_gate_does_not_promote_coverage_gaps(result, expected):
    assert exit_status(result) == expected
