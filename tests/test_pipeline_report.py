import hashlib
import importlib.util
import json
from pathlib import Path
import sys


spec = importlib.util.spec_from_file_location(
    'pipeline_report', Path(__file__).parents[1] / 'scripts/render-pipeline-report.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_inputs(tmp_path, *, valid_checksum=True):
    catalog = {
        'series': '2026.2', 'suite': 'resolute',
        'ci': {'run_id': '42', 'run_attempt': '2'},
        'packages': [
            {'source': 'alpha', 'deliverable': 'alpha', 'archive_version': '1.0-1',
             'upstream_ref': 'stable/2026.2', 'upstream_sha': 'a' * 40,
             'run_dependencies': [], 'archive_bootstrap_dependencies': [],
             'selection_reasons': ['requested']},
            {'source': 'beta', 'deliverable': 'beta', 'archive_version': '2.0-1',
             'upstream_ref': 'master', 'upstream_sha': 'b' * 40,
             'run_dependencies': ['alpha'], 'archive_bootstrap_dependencies': ['archive-helper'],
             'selection_reasons': ['upstream dependency of root']},
        ],
    }
    plan = {'sources': ['alpha', 'beta'], 'requested_sources': ['beta'],
            'waves': [['alpha'], ['beta']], 'resolution_failures': []}
    results = [
        {'source': 'alpha', 'build': {'result': 'SUCCEEDED'},
         'autopkgtest': {'result': 'NO_TESTS'}},
        {'source': 'beta', 'build': {'result': 'FAILED', 'error': '<script>bad()</script>'},
         'autopkgtest': {'result': 'BLOCKED', 'error': 'build failed'}},
    ]
    proposal_dir = tmp_path / 'proposals' / 'packaging-proposal-alpha'
    proposal_dir.mkdir(parents=True)
    patch = ('diff --git a/debian/control b/debian/control\n'
             '--- a/debian/control\n+++ b/debian/control\n'
             '@@ -1 +1 @@\n-old dependency\n+new dependency\n')
    (proposal_dir / 'packaging-proposal.patch').write_text(patch)
    digest = hashlib.sha256(patch.encode()).hexdigest() if valid_checksum else '0' * 64
    (proposal_dir / 'packaging-proposal.json').write_text(json.dumps({
        'schema_version': 1, 'source': 'alpha', 'status': 'validated',
        'human_review_required': True, 'selected_target': None,
        'destination_candidates': [{'role': 'ubuntu', 'repository': 'https://example.test/alpha'}],
        'branch_candidates': ['stable/2026.2'],
        'patch': {'file': 'packaging-proposal.patch', 'sha256': digest},
        'actions': [{'action': 'replace-packaging-file',
                     'reason': 'Upstream renamed the WSGI entry point'}],
        'removal_condition': 'Remove after review.',
        'validation': {'build': {'result': 'SUCCEEDED'},
                       'autopkgtest': {'result': 'NO_TESTS'}},
    }))
    (proposal_dir / 'source-evolution.json').write_text(json.dumps({
        'schema_version': 1, 'source': 'alpha',
        'commit_delta': {
            'comparison_tag': '1.0', 'archive_upstream_version': '1.0',
            'comparison_tag_matches_archive_version': True,
            'basis': 'official-package-upstream-tag', 'count': 1,
            'commits': [{'sha': 'c' * 40, 'subject': 'Add alpha-api command'}],
        },
        'introduced_entry_points': [{
            'group': 'console_scripts', 'name': 'alpha-api',
            'packaging': 'assigned-to-existing-binary',
            'human_binary_package_review_required': True,
        }],
        'human_binary_package_review_required': True,
    }))
    for name, value in [('catalog.json', catalog), ('plan.json', plan), ('results.json', results)]:
        (tmp_path / name).write_text(json.dumps(value))
    return proposal_dir.parent


def invoke(tmp_path, proposals, functional=None):
    output = tmp_path / 'summary'
    args = [
        '--catalog', str(tmp_path / 'catalog.json'), '--plan', str(tmp_path / 'plan.json'),
        '--results', str(tmp_path / 'results.json'), '--proposals', str(proposals),
        '--output', str(output), '--target-id', 'ubuntu-resolute-development',
        '--target-kind', 'development', '--base-series', 'resolute', '--suite', 'resolute',
    ]
    if functional:
        (tmp_path / 'functional.json').write_text(json.dumps(functional))
        args.extend(['--functional-results', str(tmp_path / 'functional.json')])
    old_argv = sys.argv
    try:
        sys.argv = ['render-pipeline-report.py', *args]
        assert module.main() == 0
    finally:
        sys.argv = old_argv
    return output


def test_report_renders_results_rationale_and_verified_inline_diff(tmp_path):
    proposals = write_inputs(tmp_path)
    output = invoke(tmp_path, proposals)
    page = (output / 'index.html').read_text()
    report = json.loads((output / 'report.json').read_text())

    assert '2026.2 snapshot report' in page
    assert 'Ubuntu development release' in page
    assert 'alpha' in page and 'beta' in page
    assert 'Upstream renamed the WSGI entry point' in page
    assert 'Add alpha-api command' in page
    assert 'alpha-api' in page
    assert 'Human review must decide whether a new binary package is needed.' in page
    assert 'diff-add">+new dependency' in page
    assert '&lt;script&gt;bad()&lt;/script&gt;' in page
    assert '<script>bad()</script>' not in page
    assert 'data-level="2"' in page
    assert 'regress-stack' in page and 'NOT_RUN' in page
    assert (output / 'patches/alpha.patch').is_file()
    assert report['target']['kind'] == 'development'
    assert report['packages'][0]['proposal']['status'] == 'validated'
    assert report['packages'][0]['source_evolution']['commit_delta']['count'] == 1
    assert report['plan']['binary_review_count'] == 1
    assert report['counts']['build'] == {'FAILED': 1, 'SUCCEEDED': 1}


def test_corrupt_patch_is_reported_and_never_exported_or_embedded(tmp_path):
    proposals = write_inputs(tmp_path, valid_checksum=False)
    output = invoke(tmp_path, proposals)
    report = json.loads((output / 'report.json').read_text())
    page = (output / 'index.html').read_text()

    assert report['packages'][0]['proposal'] is None
    assert report['warnings'][0]['error'] == 'patch checksum mismatch for alpha'
    assert not (output / 'patches/alpha.patch').exists()
    assert '+new dependency' not in page


def test_functional_gate_accepts_future_regress_stack_result(tmp_path):
    proposals = write_inputs(tmp_path)
    functional = {'name': 'regress-stack', 'result': 'PASS',
                  'summary': 'Core services passed smoke tests.'}
    output = invoke(tmp_path, proposals, functional)
    page = (output / 'index.html').read_text()

    assert 'Core services passed smoke tests.' in page
    assert 'Functional validation <span class="badge good">PASS</span>' in page
