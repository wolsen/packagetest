from pathlib import Path
import json
import os
import subprocess


def test_each_build_matrix_runs_its_own_autopkgtest():
    workflow = Path('.github/workflows/hibiscus-snapshots.yml').read_text()
    action = 'uses: ./.github/actions/test-built-snapshot'

    assert workflow.count(action) == 12
    assert 'autopkgtest_dependency_level_' not in workflow
    assert 'hibiscus-autopkgtest.yml' not in workflow
    for level in range(1, 13):
        assert f'build_dependency_level_{level}:' in workflow


def test_child_jobs_keep_direct_package_build_names():
    workflow = Path('.github/workflows/hibiscus-snapshots.yml').read_text()
    assert workflow.count('name: Build ${{ matrix.source }}') == 12


def test_integrated_test_step_records_blocked_build_and_evidence(tmp_path):
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'outputs/result.json').write_text('{"result":"FAILED"}\n')
    tools = tmp_path / 'bin'
    tools.mkdir()
    sudo = tools / 'sudo'
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    summary = tmp_path / 'summary.md'
    runner_temp = tmp_path / 'runner'
    runner_temp.mkdir()

    completed = subprocess.run(
        ['bash', str(Path('scripts/test-built-snapshot.sh').resolve()), 'sample'],
        cwd=tmp_path,
        env={
            **os.environ,
            'PATH': f'{tools}:{os.environ["PATH"]}',
            'GITHUB_RUN_ID': '123',
            'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_STEP_SUMMARY': str(summary),
            'RUNNER_TEMP': str(runner_temp),
        },
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    report = json.loads((tmp_path / 'test-results/result/result.json').read_text())
    assert report['result'] == 'BLOCKED'
    assert report['ci'] == {'run_id': '123', 'run_attempt': '1'}
    assert (tmp_path / 'test-bundles/sample.tar.gz').is_file()
    assert 'autopkgtest: BLOCKED' in summary.read_text()
