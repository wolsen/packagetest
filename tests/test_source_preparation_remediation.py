import importlib.util
import json
from pathlib import Path


path = Path(__file__).parents[1] / 'scripts/remediate-source-preparation.py'
spec = importlib.util.spec_from_file_location('source_preparation_remediation', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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


def test_cumulative_refresh_uses_upstream_tree_and_normalized_context(tmp_path):
    helper = module.remediation_module()
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
