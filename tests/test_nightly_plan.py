import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('nightly_plan', Path(__file__).parents[1] / 'scripts/nightly-plan.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def catalog():
    return {'series': '2026.2',
            'packages': [{'source': 'a', 'build_dependencies': ['b']},
                         {'source': 'b', 'build_dependencies': ['a']},
                         {'source': 'c', 'build_dependencies': ['a']},
                         {'source': 'd', 'build_dependencies': []}]}


def test_cycles_are_explicit_bootstrap_not_silently_omitted():
    frozen, plan = module.plan_catalog(catalog())
    assert plan['waves'] == [['a', 'b', 'd'], ['c']]
    assert {(e['source'], e['dependency']) for e in plan['archive_bootstrap_edges']} == {('a', 'b'), ('b', 'a')}
    assert frozen['packages'][2]['run_dependencies'] == ['a']
    assert frozen['packages'][0]['archive_bootstrap_dependencies'] == ['b']


def test_pilot_exact_selection_reports_archive_dependencies():
    frozen, plan = module.plan_catalog(catalog(), ['c'])
    assert plan['sources'] == ['c']
    assert frozen['packages'][0]['archive_bootstrap_dependencies'] == ['a']
    assert plan['archive_bootstrap_edges'][0]['reason'] == 'outside explicitly selected pilot'


def test_wave_capacity_and_unknown_source_fail():
    with pytest.raises(ValueError, match='capacity'):
        module.plan_catalog(catalog(), max_waves=1)
    with pytest.raises(ValueError, match='Unknown'):
        module.plan_catalog(catalog(), ['missing'])


def test_resolution_failure_is_individual_and_never_branch_fallback(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError('network timed out')
    monkeypatch.setattr(module.subprocess, 'run', fail)
    result = module.freeze({'source': 'a', 'upstream_repository': 'https://example.test/a', 'upstream_ref': 'stable/2026.2'})
    assert result['upstream_sha'] is None
    assert result['upstream_resolution_error'] == 'network timed out'
    assert result['upstream_ref'] == 'stable/2026.2'


def test_freeze_pins_launchpad_packaging_and_related_source_branches(monkeypatch):
    upstream_sha = '1' * 40
    packaging_sha = '2' * 40
    packaging_upstream_sha = '3' * 40
    pristine_sha = '4' * 40

    def run(command, **kwargs):
        if command[3] == 'https://opendev.org/openstack/demo':
            return SimpleNamespace(stdout=f'{upstream_sha}\trefs/heads/stable/2026.2\n',
                                   stderr='', returncode=0)
        return SimpleNamespace(stdout=(
            f'{packaging_sha}\trefs/heads/master\n'
            f'{packaging_upstream_sha}\trefs/heads/upstream-hibiscus\n'
            f'{pristine_sha}\trefs/heads/pristine-tar\n'), stderr='', returncode=0)

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze({
        'source': 'demo',
        'upstream_repository': 'https://opendev.org/openstack/demo',
        'upstream_ref': 'stable/2026.2',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'packaging_upstream_branch_candidates': ['upstream-hibiscus', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
    })

    assert entry['upstream_sha'] == upstream_sha
    assert entry['packaging_source_kind'] == 'git'
    assert entry['packaging_branch'] == 'master'
    assert entry['packaging_sha'] == packaging_sha
    assert entry['packaging_upstream_branch'] == 'upstream-hibiscus'
    assert entry['packaging_upstream_sha'] == packaging_upstream_sha
    assert entry['packaging_pristine_tar_sha'] == pristine_sha


def test_packaging_resolution_uses_archive_vcs_when_launchpad_tree_is_absent(monkeypatch):
    revision = '5' * 40

    def run(command, **kwargs):
        if 'git.launchpad.net' in command[3]:
            return SimpleNamespace(stdout='', stderr='repository does not exist', returncode=128)
        return SimpleNamespace(stdout=f'{revision}\trefs/heads/debian/hibiscus\n',
                               stderr='', returncode=0)

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze_packaging({
        'source': 'demo',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'packaging_upstream_branch_candidates': ['upstream-hibiscus', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
        'archive_packaging_repository': 'https://salsa.example/openstack/demo.git',
        'archive_packaging_branch': 'debian/hibiscus',
    })

    assert entry['packaging_source_kind'] == 'git'
    assert entry['packaging_source_role'] == 'archive-vcs'
    assert entry['packaging_source_repository'] == 'https://salsa.example/openstack/demo.git'
    assert entry['packaging_branch'] == 'debian/hibiscus'
    assert entry['packaging_sha'] == revision


def test_mandatory_candidate_restores_one_direction_of_cycle():
    policy = [{'source': 'a', 'dependency': 'b', 'reason': 'new API'}]
    frozen, plan = module.plan_catalog(catalog(), candidate_dependencies=policy)
    assert plan['waves'] == [['b', 'd'], ['a'], ['c']]
    assert {(e['source'], e['dependency']) for e in plan['archive_bootstrap_edges']} == {('b', 'a')}
    assert frozen['packages'][0]['run_dependencies'] == ['b']
    assert frozen['packages'][0]['required_candidate_dependencies'] == {'b': 'new API'}
    with pytest.raises(ValueError, match='include it'):
        module.plan_catalog(catalog(), ['a'], candidate_dependencies=policy)


def test_mandatory_candidate_cycle_cannot_silently_bootstrap():
    policy = [{'source': 'a', 'dependency': 'b', 'reason': 'new API'},
              {'source': 'b', 'dependency': 'a', 'reason': 'new API'}]
    with pytest.raises(ValueError, match='Unresolved dependency cycle'):
        module.plan_catalog(catalog(), candidate_dependencies=policy)


def test_cycle_uses_candidate_already_available_in_earlier_wave():
    data = {'packages': [{'source': 'a', 'build_dependencies': ['b', 'd']},
                         {'source': 'b', 'build_dependencies': ['a']},
                         {'source': 'd', 'build_dependencies': []}]}
    frozen, plan = module.plan_catalog(data)
    assert plan['waves'] == [['b', 'd'], ['a']]
    assert frozen['packages'][0]['run_dependencies'] == ['b', 'd']
    assert [(e['source'], e['dependency']) for e in plan['archive_bootstrap_edges']] == [('b', 'a')]


def test_plan_summary_lists_every_dependency_level_and_candidate_rule():
    policy = [{'source': 'a', 'dependency': 'b', 'reason': 'requires new | API'}]
    frozen, plan = module.plan_catalog(catalog(), candidate_dependencies=policy)
    plan['resolution_failures'] = []
    summary = module.render_plan_summary(plan, frozen)

    assert '# OpenStack 2026.2 snapshot build plan' in summary
    assert '4 source packages across 3 dependency levels' in summary
    assert '| 1 | 2 | `b`, `d` |' in summary
    assert '| 2 | 1 | `a` |' in summary
    assert '| 3 | 1 | `c` |' in summary
    assert '| `a` | `b` | requires new \\| API |' in summary
    assert 'All selected source references resolved to immutable commit SHAs.' in summary


def test_dynamic_matrix_contains_only_real_dependency_levels():
    matrix = module.dependency_level_matrix([['a', 'b'], ['c'], []])
    assert matrix == {'include': [
        {'level': 1, 'packages': '["a","b"]'},
        {'level': 2, 'packages': '["c"]'},
    ]}
    assert '__empty__' not in json.dumps(matrix)


def test_discovery_summary_defers_dependency_levels_until_preparation():
    data = {'series': '2027.1', 'series_status': 'development', 'packages': [
        {'source': 'a', 'selection_reasons': ['requested'], 'upstream_sha': '1' * 40,
         'upstream_ref': 'master', 'branch_policy': 'release-metadata-development-branch',
         'packaging_sha': '2' * 40},
    ]}
    summary = module.render_discovery_summary(data, ['a'], ['a'])
    assert '# OpenStack 2027.1 snapshot source discovery' in summary
    assert 'Series status: `development`' in summary
    assert 'selected and pinned for parallel source preparation' in summary
    assert 'Dependency levels will be computed after' in summary
    assert '| `a` | requested | `master` (release-metadata-development-branch) |' in summary


def test_upstream_metadata_expands_requested_roots_before_planning():
    data = {'packages': [
        {'source': 'ironic', 'deliverable': 'ironic', 'binaries': ['python3-ironic'],
         'build_dependencies': [], 'upstream_repository': 'https://opendev.org/openstack/ironic',
         'upstream_ref': 'master'},
        {'source': 'python-sushy', 'deliverable': 'sushy', 'binaries': ['python3-sushy'],
         'archive_version': '5.9.0-0ubuntu1', 'build_dependencies': [],
         'upstream_repository': 'https://opendev.org/openstack/sushy',
         'upstream_ref': 'stable/2026.2'},
        {'source': 'python-pbr', 'deliverable': 'pbr', 'binaries': ['python3-pbr'],
         'archive_version': '7.0.3-2', 'build_dependencies': [],
         'upstream_repository': 'https://opendev.org/openstack/pbr',
         'upstream_ref': 'master'},
        {'source': 'unrelated', 'deliverable': 'unrelated', 'binaries': ['python3-unrelated'],
         'build_dependencies': [], 'upstream_repository': 'https://opendev.org/openstack/unrelated',
         'upstream_ref': 'master'},
    ]}

    def freeze(entry):
        entry['upstream_sha'] = 'a' * 40
        return entry

    def inspect(entry):
        if entry['source'] == 'ironic':
            return ([{'distribution': 'sushy', 'requirement': 'sushy>=5.12.0',
                      'kind': 'runtime', 'file': 'requirements.txt', 'line': 1},
                     {'distribution': 'pbr', 'requirement': 'pbr>=6.0.0',
                      'kind': 'runtime', 'file': 'requirements.txt', 'line': 2}],
                    ['requirements.txt'])
        return [], ['pyproject.toml']

    enriched, selected, roots, errors = module.resolve_upstream_dependency_closure(
        data, ['ironic'], freeze_entry=freeze, inspect_entry=inspect)
    frozen, plan = module.plan_catalog(enriched, selected)
    entries = {entry['source']: entry for entry in frozen['packages']}

    assert roots == ['ironic']
    assert selected == ['ironic', 'python-sushy']
    assert errors == []
    assert plan['waves'] == [['python-sushy'], ['ironic']]
    assert entries['ironic']['run_dependencies'] == ['python-sushy']
    assert entries['python-sushy']['selection_reasons'] == ['upstream dependency of ironic']
    assert entries['ironic']['upstream_dependency_requirements'][0]['source'] == 'python-sushy'
    assert entries['ironic']['upstream_dependency_requirements'][1]['archive_decision'] == 'satisfied'


def test_upstream_external_test_dependency_is_annotated_without_dag_node():
    data = {
        'archive_python_packages': [{
            'distribution': 'wsgi-intercept', 'binary': 'python3-wsgi-intercept',
            'source': 'python-wsgi-intercept', 'version': '1.13.1-1',
        }],
        'packages': [{
            'source': 'watcher', 'deliverable': 'watcher', 'binaries': ['python3-watcher'],
            'build_dependencies': [], 'upstream_repository': 'https://opendev.org/openstack/watcher',
            'upstream_ref': 'master',
        }],
    }

    def freeze(entry):
        entry['upstream_sha'] = 'a' * 40
        return entry

    def inspect(entry):
        return ([{'distribution': 'wsgi-intercept', 'requirement': 'wsgi-intercept>=1.7',
                  'kind': 'test', 'file': 'test-requirements.txt', 'line': 1}],
                ['test-requirements.txt'])

    enriched, selected, _, errors = module.resolve_upstream_dependency_closure(
        data, ['watcher'], freeze_entry=freeze, inspect_entry=inspect)
    record = enriched['packages'][0]['upstream_dependency_requirements'][0]
    assert selected == ['watcher']
    assert errors == []
    assert record['archive_binary'] == 'python3-wsgi-intercept'
    assert record['archive_decision'] == 'satisfied'
    assert enriched['packages'][0]['planned_archive_dependency_additions'] == [{
        'binary': 'python3-wsgi-intercept', 'source': 'python-wsgi-intercept',
        'archive_version': '1.13.1-1', 'kind': 'test',
        'requirement': 'wsgi-intercept>=1.7', 'file': 'test-requirements.txt', 'line': 1,
    }]
