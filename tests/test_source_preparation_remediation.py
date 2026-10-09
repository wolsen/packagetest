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
