import builtins

from packagetest.locked import LockedBuild, StageFailure
import pytest


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
    from packagetest.artifacts import fields
    from packagetest import nightly_build as module
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
