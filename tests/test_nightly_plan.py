import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('nightly_plan', Path(__file__).parents[1] / 'scripts/nightly-plan.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def catalog():
    return {'packages': [{'source': 'a', 'build_dependencies': ['b']},
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


def test_reviewed_control_adds_independent_build_dependency_to_graph(tmp_path):
    entries = {'packages': [
        {'source': 'client', 'binaries': ['python3-client'], 'build_depends': 'debhelper',
         'build_dependencies': [], 'archive_source': {'sha256': 'archive-digest'}},
        {'source': 'library', 'binaries': ['python3-library'], 'build_depends': 'debhelper',
         'build_dependencies': []}]}
    root = tmp_path / 'client'
    root.mkdir()
    control = root / 'control'
    control.write_text('Source: client\nBuild-Depends: debhelper\n'
                       'Build-Depends-Indep: python3-library (>= 2.0)\n')
    (root / 'adjustments.json').write_text(json.dumps({
        'archive_dsc_sha256': 'archive-digest', 'replace_files': [{
            'name': 'control', 'replacement': 'control',
            'replacement_sha256': hashlib.sha256(control.read_bytes()).hexdigest()}]}))
    effective = module.apply_packaging_dependencies(entries, tmp_path)
    assert effective['packages'][0]['build_dependencies'] == ['library']
    assert effective['packages'][0]['archive_build_depends'] == 'debhelper'
    assert module.plan_catalog(effective)[1]['waves'] == [['library'], ['client']]
    control.write_text(control.read_text() + '# changed\n')
    with pytest.raises(ValueError, match='checksum mismatch'):
        module.apply_packaging_dependencies(entries, tmp_path)
