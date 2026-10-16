"""Exercise source repair evidence and deterministic preparation retries."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from packagetest import source_remediation as module


def test_failed_tree_and_quilt_evidence_are_selected(tmp_path):
    work = tmp_path / 'work'
    tree = work / 'glance-33.0' / 'debian'
    logs = work / 'logs'
    output = tmp_path / 'prepared-source'
    tree.mkdir(parents=True)
    logs.mkdir()
    output.mkdir()
    (tree / 'control').write_text('Source: glance\n')
    (work / 'resolution.json').write_text(json.dumps({
        'source': 'glance', 'status': 'FAILED', 'error': 'dpkg-buildpackage failed'}))
    (output / 'result.json').write_text(json.dumps({'result': 'FAILED'}))
    (logs / '0031.stderr.log').write_text(
        'applying fix-test.patch\nHunk #2 FAILED\ndpkg-source: error\n')
    (logs / 'unrelated.log').write_text('ordinary command output\n')

    assert module.failed_tree(work, 'glance') == tree.parent
    evidence = module.failure_evidence(work, output)
    assert 'fix-test.patch' in evidence
    assert 'Hunk #2 FAILED' in evidence
    assert 'ordinary command output' not in evidence


def test_failed_quilt_patch_is_selected_for_deterministic_refresh(tmp_path):
    patches = tmp_path / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('first.patch\nentry-point.patch\n')
    evidence = (
        'dpkg-source: info: applying first.patch\n'
        'dpkg-source: info: applying entry-point.patch\n'
        'Hunk #1 FAILED at 31.\n'
        'debian/patches/entry-point.patch subprocess returned exit status 1\n')
    assert module.failed_quilt_patches(tmp_path, evidence) == ['entry-point.patch']


def test_failed_quilt_patch_prefers_final_dpkg_source_error(tmp_path):
    patches = tmp_path / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('first.patch\nsecond.patch\n')
    evidence = (
        'dpkg-source: info: applying first.patch\n'
        '.pc/first.patch/file contains an earlier backup\n'
        'dpkg-source: info: applying second.patch\n'
        'Hunk #1 FAILED\n'
        'dpkg-source: error: patch < debian/patches/second.patch subprocess returned exit status 1\n')
    assert module.failed_quilt_patches(tmp_path, evidence) == ['second.patch']


def test_cumulative_refresh_uses_upstream_tree_and_normalized_context(tmp_path):
    from packagetest import remediation as helper
    patches = tmp_path / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('entry-point.patch\n')
    (patches / 'entry-point.patch').write_text('''Description: expose config options
--- heat-tempest-plugin-2.5.0.orig/setup.cfg
+++ heat-tempest-plugin-2.5.0/setup.cfg
@@ -1,3 +1,6 @@
 [entry_points]
 tempest.test_plugins =
     heat = heat_tempest_plugin.plugin:HeatTempestPlugin
+
+oslo.config.opts =
+    heat-tempest-plugin = heat_tempest_plugin.config:list_opts
''')
    (tmp_path / 'setup.cfg').write_text(
        '[entry_points]\n'
        'tempest.test_plugins =' + ' \n'
        '\theat = heat_tempest_plugin.plugin:HeatTempestPlugin\n\n'
        '[egg_info]\n'
        'tag_build =' + ' \n')
    decision = {
        'action': 'refresh_quilt_patch', 'subject': 'entry-point.patch',
        'replacement': '', 'evidence': 'entry-point.patch Hunk #1 FAILED',
    }
    patch = helper.render_cumulative_repair([decision], tmp_path)
    assert 'diff --git a/debian/patches/entry-point.patch' in patch
    validation = helper.validate_source_patch(patch, tmp_path)
    assert validation['result'] == 'APPLIES'


@pytest.mark.parametrize('state', ['context-drift', 'superseded', 'unrepairable'])
def test_deterministic_repair_reaches_preparation_without_model(tmp_path, monkeypatch, state):
    """Exercise refresh, supersession, and error reporting through the CLI boundary."""
    from packagetest import remediation as helper

    work = tmp_path / 'initial'
    tree = work / 'glance-33.0'
    patches = tree / 'debian/patches'
    patches.mkdir(parents=True)
    (tree / 'debian/control').write_text('Source: glance\n')
    (patches / 'series').write_text('offline-test.patch\n')
    (patches / 'offline-test.patch').write_text('''--- a/test_image.py
+++ b/test_image.py
@@ -1,4 +1,5 @@
 # original context
 class TestImage:
-    def test_location(self):
+    @mock.patch('socket.getaddrinfo')
+    def test_location(self, mock_getaddrinfo):
         pass
''')
    contents = {
        'context-drift': '# changed context\nclass TestImage:\n    def test_location(self):\n        pass\n',
        'superseded': '# changed context\nclass TestImage:\n    @mock.patch("socket.getaddrinfo")\n    def test_location(self, mock_getaddrinfo):\n        pass\n',
        'unrepairable': '# changed context\nclass TestImage:\n    def test_location(self):\n        pass\n',
    }
    (tree / 'test_image.py').write_text(contents[state])

    if state == 'unrepairable':
        (patches / 'offline-test.patch').write_text('not a unified patch\n')

    output = tmp_path / 'output'
    output.mkdir()
    (output / 'result.json').write_text(json.dumps({
        'result': 'FAILED',
        'error': 'dpkg-source: error: patch < debian/patches/offline-test.patch failed',
    }))
    model = tmp_path / 'model'
    model.write_bytes(b'fixture model, never invoked')
    report = tmp_path / 'report'
    monkeypatch.setattr(sys, 'argv', [
        'remediate-source-preparation', '--source', 'glance',
        '--catalog', str(tmp_path / 'catalog.json'), '--initial-work', str(work),
        '--output', str(output), '--report', str(report), '--work', str(tmp_path / 'retry'),
        '--llama-cli', str(tmp_path / 'unused-llama'), '--model', str(model),
        '--model-sha256', helper.sha256(model), '--run-id', '123', '--run-attempt', '1',
        '--max-model-calls', '0',
    ])
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    original_run = subprocess.run
    preparations = []

    def prepare_candidate(command, **kwargs):
        """Use real patch tools while replacing the expensive source-build boundary."""
        if command[:3] != [sys.executable, '-m', 'packagetest.prepared_source']:
            return original_run(command, **kwargs)

        candidate = Path(command[command.index('--work') + 1])
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tree, candidate)
        proposal = Path(command[command.index('--remediation-patch') + 1])
        assert helper.validate_source_patch(proposal.read_text(), candidate, apply=True)['result'] == 'APPLIED'
        series = candidate / 'debian/patches/series'

        if state == 'context-drift':
            # The refreshed patch must satisfy dpkg-source's zero-fuzz policy.
            result = original_run(
                ['patch', '--batch', '--fuzz=0', '-p1', '-i', str(candidate / 'debian/patches/offline-test.patch')],
                cwd=candidate, capture_output=True, text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "@mock.patch('socket.getaddrinfo')" in (candidate / 'test_image.py').read_text()
        else:
            assert series.read_text().startswith('# Superseded upstream')
            assert (candidate / 'test_image.py').read_text() == contents[state]

        candidate_output = Path(command[command.index('--output') + 1])
        candidate_output.mkdir()
        (candidate_output / 'prepared-source.json').write_text('{}')
        preparations.append(candidate)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, 'run', prepare_candidate)
    result = module.main()
    saved = json.loads((report / 'result.json').read_text())
    attempt = saved['attempts'][0]
    assert len(saved['attempts']) == 1
    assert (patches / 'series').read_text() == 'offline-test.patch\n'

    if state == 'unrepairable':
        assert result == 1
        assert preparations == []
        assert 'requires unified patch sections' in attempt['error']
        assert 'QuiltPatchSuperseded' not in attempt['error']
        assert saved['result'] == 'UNRESOLVED'
        return

    assert result == 0
    assert len(preparations) == 1
    assert saved['selected_attempt'] == 'D1'
    assert attempt['result'] == 'REPAIRED'
    assert (output / 'prepared-source.json').is_file()
    expected_action = 'refresh_quilt_patch' if state == 'context-drift' else 'drop_quilt_patch'
    assert attempt['decision']['action'] == expected_action

    if state == 'superseded':
        assert attempt['classification'] == 'superseded-upstream'
        assert 'old patch payload no longer exists' in attempt['classification_evidence']


@pytest.mark.parametrize('outcome', ['no_fix', 'rejected', 'prepared', 'preparation_failed'])
def test_model_fallback_preserves_attempt_evidence(tmp_path, monkeypatch, outcome):
    """Fallback records validation failures, explicit no-fix decisions, and preparation results."""
    import argparse

    from packagetest import remediation as helper

    initial = tmp_path / 'initial'
    tree = initial / 'glance-33.0'
    (tree / 'debian').mkdir(parents=True)
    (tree / 'debian/control').write_text('Source: glance\n')
    output = tmp_path / 'output'
    output.mkdir()
    report = tmp_path / 'report'
    report.mkdir()
    args = argparse.Namespace(
        initial_work=initial, source='glance', output=output, report=report,
        work=tmp_path / 'retry', catalog=tmp_path / 'catalog.json', run_id='123', run_attempt='1',
        llama_cli=tmp_path / 'llama', model=tmp_path / 'model', model_timeout=180, max_model_calls=1,
    )
    decision = {'action': 'no_fix' if outcome == 'no_fix' else 'refresh_quilt_patch',
                'subject': 'test.patch', 'replacement': '', 'evidence': 'failed patch'}
    monkeypatch.setattr(helper, 'llama_generate', lambda *args: (json.dumps(decision), {'backend': 'fixture'}))
    monkeypatch.setattr(module, 'normalize_repair_decision', lambda decision, *args: decision)
    validation = {'result': 'REJECTED', 'error': 'unsafe repair'} if outcome == 'rejected' else {'result': 'ACCEPTED'}
    monkeypatch.setattr(module, 'validate_repair_decision', lambda *args: validation)
    monkeypatch.setattr(helper, 'render_cumulative_repair', lambda *args: 'fixture patch')
    monkeypatch.setattr(module, 'validate_source_patch', lambda *args: {'result': 'APPLIES'})

    def prepare(command, **kwargs):
        """Produce only the preparation artifacts needed to exercise promotion and retries."""
        candidate = Path(command[command.index('--output') + 1])
        candidate.mkdir(parents=True)
        if outcome == 'prepared':
            (candidate / 'prepared-source.json').write_text('{}')
            return subprocess.CompletedProcess(command, 0)

        (candidate / 'result.json').write_text('{"error": "another patch failed"}')
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(module.subprocess, 'run', prepare)
    session = module.SourceRepairSession(args)
    session.model_repairs()

    assert len(session.attempts) == 1
    record = session.attempts[0]
    assert record['result'] == {
        'no_fix': 'NO_FIX', 'rejected': 'MODEL_ERROR',
        'prepared': 'REPAIRED', 'preparation_failed': 'PREPARATION_FAILED',
    }[outcome]
    assert json.loads((report / 'attempt-1/decision-validation.json').read_text()) == validation
    assert session.selected == (1 if outcome == 'prepared' else None)
    assert (output / 'prepared-source.json').exists() == (outcome == 'prepared')

    if outcome == 'preparation_failed':
        assert 'another patch failed' in session.evidence
        assert json.loads(session.feedback) == record

    if outcome == 'rejected':
        assert record['error'] == 'unsafe repair'
        assert session.decisions == []
