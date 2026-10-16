import json
from pathlib import Path

import pytest

from packagetest.autopkgtest import (checked_file, classify, collect_test_diagnostics,
                                     diagnostic_message, exit_status, run)


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


def test_diagnostic_message_explains_failed_test_behind_return_code():
    report = classify(4, 'debtcollector-import FAIL python exited with status 1\n')
    assert diagnostic_message(report) == (
        'debtcollector-import FAIL: python exited with status 1'
    )


def test_diagnostic_message_preserves_infrastructure_error():
    assert diagnostic_message({'error': 'testbed setup timed out'}) == 'testbed setup timed out'


def test_failed_test_diagnostic_includes_bounded_stderr_and_stdout(tmp_path):
    (tmp_path / 'autodep8-python3-stderr').write_text('Traceback: wrong import\n')
    (tmp_path / 'autodep8-python3-stdout').write_text('Testing with python3\n')
    tests = [{'name': 'autodep8-python3', 'result': 'FAIL', 'detail': 'non-zero'}]

    diagnostics = collect_test_diagnostics(tmp_path, tests)

    assert diagnostics == {
        'autodep8-python3': (
            'stderr: Traceback: wrong import; stdout: Testing with python3')}
    assert 'Traceback: wrong import' in diagnostic_message({
        'tests': tests, 'test_diagnostics': diagnostics})


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
    ('PASS', 0), ('SUPERFICIAL', 0), ('SKIP', 0), ('NO_TESTS', 0),
    ('FAIL', 1), ('INFRA_ERROR', 1), ('BLOCKED', 1),
])
def test_ci_exit_status_keeps_coverage_gaps_non_fatal(result, expected):
    assert exit_status(result) == expected


@pytest.mark.parametrize('raw,summary,classification', [
    (8, '* SKIP no tests in this package\n', 'NO_TESTS'),
    (8, 'autodep8-python3 PASS (superficial)\n', 'SUPERFICIAL'),
])
def test_autopkgtest_return_8_is_reported_but_does_not_fail_ci(raw, summary, classification):
    report = classify(raw, summary)
    assert report == {
        'result': classification,
        'returncode': 8,
        'tests': [{
            'name': '*' if classification == 'NO_TESTS' else 'autodep8-python3',
            'result': 'SKIP' if classification == 'NO_TESTS' else 'PASS',
            'detail': 'no tests in this package' if classification == 'NO_TESTS' else '(superficial)',
        }],
    }
    assert exit_status(report['result']) == 0


@pytest.mark.parametrize('database,result,installed', [
    ('nova-compute-kvm\t2:34.0+git1\tinstalled\nnova-compute-ironic\t2:33.0\tconfig-files\n', 0, {'nova-compute-kvm': '2:34.0+git1'}),
    ('nova-compute-ironic\t2:34.0+git1\tunpacked\n', 0, {'nova-compute-ironic': '2:34.0+git1'}),
    ('nova-compute-ironic\t2:34.0+git1\thalf-configured\n', 0, {'nova-compute-ironic': '2:34.0+git1'}),
    ('nova-compute-kvm\t2:33.0\tinstalled\n', 1, {'nova-compute-kvm': '2:33.0'}),
    ('nova-compute-kvm\t2:33.0\tunpacked\n', 1, {'nova-compute-kvm': '2:33.0'}),
    ('nova-compute-kvm\t2:33.0\tnot-installed\n', 0, {}),
    ('unrelated\t1.0\tinstalled\n', 0, {}),
    ('nova-compute-kvm:amd64\t2:34.0+git1\tinstalled\n', 0, {'nova-compute-kvm': '2:34.0+git1'}),
])
def test_candidate_hook_supports_variant_switches_and_rejects_archive_fallback(tmp_path, database, result, installed):
    import os
    import subprocess
    import sys
    from packagetest.autopkgtest import candidate_check_script
    query = tmp_path / 'dpkg-query'
    query.write_text('#!' + sys.executable + '\nprint(' + repr(database) + ', end="")\n')
    query.chmod(0o755)
    script = tmp_path / 'verify.py'
    script.write_text(candidate_check_script({'nova-compute-kvm': '2:34.0+git1', 'nova-compute-ironic': '2:34.0+git1'}))
    completed = subprocess.run([sys.executable, str(script)], env={**os.environ, 'PATH': str(tmp_path)}, capture_output=True, text=True)
    assert completed.returncode == result, completed.stderr
    assert json.loads(completed.stdout.removeprefix('PACKAGETEST_INSTALLED_CANDIDATES ')) == installed
    if result:
        assert 'Candidate version mismatch' in completed.stderr


def test_timeout_kills_and_reaps_testbed_process_group(tmp_path, monkeypatch):
    """A timeout must stop testbed descendants before the report records failure."""
    import signal
    import subprocess
    from unittest.mock import MagicMock
    from packagetest import autopkgtest

    process = MagicMock()
    process.__enter__.return_value = process
    process.pid = 1234
    process.wait.side_effect = [subprocess.TimeoutExpired('autopkgtest', 3601), 0]
    monkeypatch.setattr(autopkgtest.subprocess, 'Popen', lambda *args, **kwargs: process)
    killed = []
    monkeypatch.setattr(autopkgtest.os, 'killpg', lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(subprocess.TimeoutExpired):
        autopkgtest._execute_test(['autopkgtest'], tmp_path, 1)

    assert killed == [(1234, signal.SIGKILL)]
    assert process.wait.call_args_list[0].kwargs == {'timeout': 3601}
    assert process.wait.call_args_list[1].kwargs == {}


def test_dependency_repository_rejects_escaped_candidate(tmp_path):
    """Valid bytes outside the dependency repository must never enter the testbed."""
    from packagetest import autopkgtest

    repository = tmp_path / 'repository'
    repository.mkdir()
    escaped = tmp_path / 'escaped.deb'
    escaped.write_bytes(b'candidate')
    record = {'path': '../escaped.deb', 'sha256': autopkgtest.sha256(escaped)}
    (repository / 'provenance.json').write_text(json.dumps({'packages': [record]}))

    with pytest.raises(ValueError, match='Corrupt dependency repository artifact'):
        autopkgtest._merge_dependencies({}, repository)


def test_dependency_repository_rejects_conflicting_filename(tmp_path):
    """Dependency provenance cannot replace a build candidate with different bytes."""
    from packagetest import autopkgtest

    repository = tmp_path / 'repository'
    repository.mkdir()
    candidate = tmp_path / 'candidate.deb'
    candidate.write_bytes(b'build')
    dependency = repository / candidate.name
    dependency.write_bytes(b'dependency')
    record = {'path': dependency.name, 'sha256': autopkgtest.sha256(dependency)}
    (repository / 'provenance.json').write_text(json.dumps({'packages': [record]}))

    with pytest.raises(ValueError, match='Conflicting candidate'):
        autopkgtest._merge_dependencies({candidate.name: candidate}, repository)
