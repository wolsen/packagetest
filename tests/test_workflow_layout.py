from pathlib import Path
import json
import os
import subprocess


def test_each_build_matrix_runs_its_own_autopkgtest():
    workflow = Path('.github/workflows/hibiscus-snapshots.yml').read_text()
    action = 'uses: ./.github/actions/build-test-remediate'

    assert workflow.count(action) == 12
    assert 'autopkgtest_dependency_level_' not in workflow
    assert 'hibiscus-autopkgtest.yml' not in workflow
    for level in range(1, 13):
        assert f'build_dependency_level_{level}:' in workflow


def test_child_jobs_keep_direct_package_build_names():
    workflow = Path('.github/workflows/hibiscus-snapshots.yml').read_text()
    assert workflow.count('name: Build ${{ matrix.source }}') == 12


def test_local_ai_repairs_inside_each_package_job_before_publication():
    workflow = Path('.github/workflows/hibiscus-snapshots.yml').read_text()
    action = Path('.github/actions/build-test-remediate/action.yml').read_text()
    assert 'failure_analysis_plan:' not in workflow
    assert 'analyze_failure:' not in workflow
    assert workflow.count('uses: ./.github/actions/build-test-remediate') == 12
    assert 'needs: [plan, prepare_local_ai]' in workflow
    assert 'scripts/remediate-package.py' in action
    assert action.index('id: initial_test') < action.index('id: ai_cache')
    assert action.index('id: ai_cache') < action.index('id: remediation')
    assert action.index('scripts/remediate-package.py') < action.index('name: build-${{ inputs.source }}')
    assert 'name: ai-remediation-${{ inputs.source }}' in action
    assert 'scripts/final-package-verdict.py' in action
    assert 'qwen2.5-coder-7b-instruct-q4_k_m.gguf' in workflow
    assert 'Reply with READY.' in workflow
    assert 'local-ai-llama-b10964-qwen25-coder-7b-q4km-v1' in workflow


def test_feature_push_exercises_heat_candidate_closure():
    workflow = Path('.github/workflows/hibiscus-snapshots.yml').read_text()
    policy = json.loads(Path('config/hibiscus-candidate-dependencies.json').read_text())
    assert ("github.event_name == 'push' && "
            "!contains(github.event.head_commit.message, '[full-snapshot]') && "
            "'python-neutron-lib,python-oslo.versionedobjects,heat,watcher'") in workflow
    assert "or empty manual dispatch selects the full catalog" in workflow
    assert any(edge['source'] == 'heat' and edge['dependency'] == 'python-neutron-lib'
               for edge in policy)
    assert any(edge['source'] == 'heat' and edge['dependency'] == 'python-oslo.versionedobjects'
               for edge in policy)


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
