import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'finalize_packaging_proposal',
    Path(__file__).parents[1] / 'scripts/finalize-packaging-proposal.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_successful_build_marks_human_review_proposal_validated(tmp_path):
    proposal = tmp_path / 'proposal.json'
    build = tmp_path / 'build.json'
    test = tmp_path / 'test.json'
    proposal.write_text(json.dumps({
        'status': 'candidate', 'human_review_required': True, 'selected_target': None}))
    build.write_text(json.dumps({'result': 'SUCCEEDED', 'stage': 'binary-build'}))
    test.write_text(json.dumps({'result': 'NO_TESTS', 'returncode': 8, 'ci': {'run_id': '123'}}))

    result = module.finalize(proposal, build, test)

    assert result['status'] == 'validated'
    assert result['human_review_required'] is True
    assert result['selected_target'] is None
    assert result['validation']['autopkgtest']['result'] == 'NO_TESTS'
    assert json.loads(proposal.read_text()) == result


def test_failed_validation_remains_candidate_and_missing_proposal_is_noop(tmp_path):
    proposal = tmp_path / 'proposal.json'
    build = tmp_path / 'build.json'
    test = tmp_path / 'test.json'
    proposal.write_text(json.dumps({'status': 'candidate'}))
    build.write_text(json.dumps({'result': 'FAILED', 'error': 'build failed'}))
    test.write_text(json.dumps({'result': 'BLOCKED'}))

    assert module.finalize(proposal, build, test)['status'] == 'candidate'
    assert module.finalize(tmp_path / 'missing.json', build, test) is None
