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
