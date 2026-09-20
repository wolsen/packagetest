import importlib.util
import json
from pathlib import Path

from packagetest.locked import LockedBuild, StageFailure
import pytest


def test_prepared_snapshot_is_rechecked_before_build(tmp_path):
    dsc = tmp_path / 'sample.dsc'
    dsc.write_text('corrupted after resolution')
    lock = {'target': {}, 'packages': []}
    build = LockedBuild(lock, tmp_path / 'output', prepared_sources={'sample': dsc})
    package = {'source': 'sample', 'version': '1.0-1', 'input': {'dsc_sha256': '0' * 64}}
    with pytest.raises(StageFailure, match='checksum mismatch'):
        build.prepared_source(package, tmp_path / 'destination')


def test_transitive_same_run_dependencies():
    path = Path(__file__).parents[1] / 'scripts/nightly-build.py'
    spec = importlib.util.spec_from_file_location('nightly_build', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entries = {'a': {'run_dependencies': []},
               'b': {'run_dependencies': ['a']},
               'c': {'run_dependencies': ['b', 'a'], 'archive_bootstrap_dependencies': ['d']}}
    assert module.dependencies(entries, 'c') == {'a', 'b'}


def test_adapted_indep_dependency_is_pinned_to_candidate(tmp_path):
    from packagetest.artifacts import fields
    path = Path(__file__).parents[1] / 'scripts/nightly-build.py'
    spec = importlib.util.spec_from_file_location('nightly_build', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dsc = tmp_path / 'adapted.dsc'
    dsc.write_text('Source: client\nBuild-Depends: debhelper\n'
                   'Build-Depends-Indep: python3-sdk (>= 4.19.0)\n'
                   'Build-Depends-Arch: python3-helper\n')
    producers = {'sdk': [{'package': 'python3-sdk', 'version': '4.20.0+git1'},
                         {'package': 'python-sdk-doc', 'version': '4.20.0+git1'}],
                 'helper': [{'package': 'python3-helper', 'version': '2.0+git1'}]}
    assert module.required_versions(fields(dsc), producers) == {
        'python3-sdk': '4.20.0+git1', 'python3-helper': '2.0+git1'}
