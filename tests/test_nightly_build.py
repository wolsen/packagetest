import builtins

import pytest

from packagetest.locked import LockedBuild, StageFailure


def test_dependency_preflight_does_not_import_source_preparation(monkeypatch):
    real_import = builtins.__import__

    def reject_source_preparation(name, *args, **kwargs):
        if name == 'packagetest.nightly_source':
            raise AssertionError('dependency preflight imported source preparation')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', reject_source_preparation)
    import importlib

    from packagetest import nightly_build
    importlib.reload(nightly_build)


def test_prepared_snapshot_is_rechecked_before_build(tmp_path):
    dsc = tmp_path / 'sample.dsc'
    dsc.write_text('corrupted after resolution')
    lock = {'target': {}, 'packages': []}
    build = LockedBuild(lock, tmp_path / 'output', prepared_sources={'sample': dsc})
    package = {'source': 'sample', 'version': '1.0-1', 'input': {'dsc_sha256': '0' * 64}}
    with pytest.raises(StageFailure, match='checksum mismatch'):
        build.prepared_source(package, tmp_path / 'destination')


def test_transitive_same_run_dependencies():
    from packagetest import nightly_build as module
    entries = {'a': {'run_dependencies': []},
               'b': {'run_dependencies': ['a']},
               'c': {'run_dependencies': ['b', 'a'], 'archive_bootstrap_dependencies': ['d']}}
    assert module.dependencies(entries, 'c') == {'a', 'b'}


def test_adapted_indep_dependency_is_pinned_to_candidate(tmp_path):
    from packagetest import nightly_build as module
    from packagetest.artifacts import fields
    dsc = tmp_path / 'adapted.dsc'
    dsc.write_text('Source: client\nBuild-Depends: debhelper\n'
                   'Build-Depends-Indep: python3-sdk (>= 4.19.0)\n'
                   'Build-Depends-Arch: python3-helper\n')
    producers = {'sdk': [{'package': 'python3-sdk', 'version': '4.20.0+git1'},
                         {'package': 'python-sdk-doc', 'version': '4.20.0+git1'}],
                 'helper': [{'package': 'python3-helper', 'version': '2.0+git1'}]}
    assert module.required_versions(fields(dsc), producers) == {
        'python3-sdk': '4.20.0+git1', 'python3-helper': '2.0+git1'}


def test_prepared_dsc_is_binary_package_source_of_truth():
    from packagetest import nightly_build as module
    assert module.declared_binaries({'Binary': 'python3-demo, demo-api'}) == [
        'demo-api', 'python3-demo']
    with pytest.raises(ValueError, match='declares no binary'):
        module.declared_binaries({})


def test_repair_cannot_add_an_unplanned_candidate_edge():
    from packagetest.nightly_build import validate_candidate_dependencies
    from packagetest.relations import BuildContext
    entry = {'source': 'consumer', 'binaries': ['python3-consumer']}
    entries = {'consumer': entry, 'producer': {'binaries': ['python3-producer']}}
    with pytest.raises(ValueError, match='replan required'):
        validate_candidate_dependencies(entry, entries,
                                        {'Build-Depends': 'python3-producer'}, set(), BuildContext())
    validate_candidate_dependencies(entry, entries,
                                    {'Build-Depends': 'python3-producer [arm64]'}, set(), BuildContext())


def test_repair_preserves_previously_planned_archive_alternative():
    from packagetest.nightly_build import validate_candidate_dependencies
    from packagetest.relations import BuildContext
    relationships = {'Build-Depends': 'python3-producer (>= 9) | system-library'}
    entry = {'source': 'consumer', 'exact_build_depends': relationships}
    entries = {'consumer': entry, 'producer': {'binaries': ['python3-producer']}}
    validate_candidate_dependencies(entry, entries, relationships, set(), BuildContext())


def test_named_build_inputs_reject_unknown_producers():
    """Build context validation must reject references outside its frozen catalog."""
    from packagetest.nightly_build import BuildInputs

    with pytest.raises(ValueError, match='outside the frozen catalog'):
        BuildInputs('demo', {}, {'suite': 'noble', 'architecture': 'amd64'},
                    {'run_id': '1', 'run_attempt': '1'}, {'demo': {'source': 'demo'}},
                    frozenset({'missing'}))


def test_prepared_build_record_preserves_lock_identity_and_reuse_version_gate(tmp_path):
    """Named source records retain lock content and enforce frozen reused versions."""
    from packagetest.nightly_build import (
        BuildInputs,
        PreparedBuildSource,
        _prepared_build_lock,
    )

    dsc = tmp_path / 'demo.dsc'
    dsc.write_text('Source: demo\nVersion: 2.0-1\nBinary: python3-demo\n')
    entry = {'source': 'demo', 'upstream_sha': 'a' * 40, 'prepared_version': '2.0-1'}
    inputs = BuildInputs('demo', {}, {'suite': 'noble', 'architecture': 'amd64'},
                         {'run_id': '1', 'run_attempt': '1'}, {'demo': entry}, frozenset())
    prepared = PreparedBuildSource.from_preparation({'dsc': str(dsc), 'metadata': {}})
    lock = _prepared_build_lock(inputs, prepared, {})
    assert lock['catalog_entry'] == entry
    assert lock['target'] == inputs.target
    assert lock['packages'][0]['expected_binaries'] == ['python3-demo']
    assert lock['packages'][0]['version'] == '2.0-1'
    assert inputs.context.architecture == 'amd64'

    entry['prepared_version'] = '1.0-1'
    with pytest.raises(ValueError, match='version differs'):
        _prepared_build_lock(inputs, PreparedBuildSource(dsc, {}, reused=True), {})


def test_dependency_only_preflight_never_prepares_source(tmp_path, monkeypatch):
    """Named build context must preserve the early handoff-only execution path."""
    from argparse import Namespace

    from packagetest import nightly_build as module

    inputs = module.BuildInputs(
        'demo', {}, {'suite': 'noble', 'architecture': 'amd64'},
        {'run_id': '1', 'run_attempt': '1'}, {'demo': {'source': 'demo'}}, frozenset())
    monkeypatch.setattr(module, '_build_inputs', lambda args: inputs)
    monkeypatch.setattr(module, 'collect_producers', lambda *args, **kwargs: {})

    def unexpected(*args, **kwargs):
        """Fail if dependency preflight reaches source preparation."""
        raise AssertionError('Source preparation must follow dependency preflight')

    monkeypatch.setattr(module, '_prepare_build_source', unexpected)
    args = Namespace(inputs=tmp_path, run_id='1', run_attempt='1', check_dependencies_only=True)
    state = {'result': 'PREPARING', 'stage': 'dependency-handoff'}
    assert module._execute_build(args, state) == 0
    assert state['result'] == 'READY'


def test_absent_unaccepted_producer_is_recorded_as_dependency_block(tmp_path, monkeypatch):
    """Omitting failed producer archives must preserve the dependency-only blocked verdict."""
    from argparse import Namespace

    from packagetest import nightly_build as module

    inputs = module.BuildInputs(
        'consumer', {}, {'suite': 'noble', 'architecture': 'amd64'},
        {'run_id': '1', 'run_attempt': '1'},
        {'consumer': {'source': 'consumer'}, 'producer': {'source': 'producer'}},
        frozenset({'producer'}))
    monkeypatch.setattr(module, '_build_inputs', lambda args: inputs)
    state = {'result': 'PREPARING', 'stage': 'dependency-handoff'}
    with pytest.raises(ValueError, match='Missing dependency builds'):
        module._execute_build(Namespace(inputs=tmp_path), state)

    assert state['result'] == 'BLOCKED'
    assert state['stage'] == 'dependency-handoff'


def test_dependency_closure_rejects_a_string_instead_of_producer_names():
    """A malformed graph must not traverse individual characters as source names."""
    from packagetest.nightly_build import dependencies

    with pytest.raises(ValueError, match='must be a list'):
        dependencies({'demo': {'run_dependencies': 'producer'}}, 'demo')


def test_fresh_preparation_rejects_nonobject_evidence():
    """Lock construction requires named preparation evidence rather than a JSON array."""
    from packagetest.nightly_build import PreparedBuildSource

    with pytest.raises(ValueError, match='must be an object'):
        PreparedBuildSource.from_preparation({'dsc': 'demo.dsc', 'metadata': []})
