import pytest

from packagetest.contracts import BuildTarget, PreparedSource, RemediationSession


def test_target_validates_profile_shape():
    with pytest.raises(ValueError, match='list of profile names'):
        BuildTarget.from_mapping({'suite': 'noble', 'architecture': 'amd64',
                                  'build_profiles': 'cross'})


def test_prepared_source_rejects_escaping_filename():
    with pytest.raises(ValueError, match='filename'):
        PreparedSource.from_mapping({'schema_version': 1, 'source': 'sample',
                                     'version': '1', 'dsc': '../sample.dsc',
                                     'dsc_sha256': 'a' * 64, 'catalog_entry_sha256': 'b' * 64})


def test_session_bounds_each_phase_and_stops_at_deadline():
    session = RemediationSession(deadline=100, max_model_calls=3, max_rebuilds=2, max_repairs=4)
    assert session.timeout(60, now=90) == 10
    assert session.stop_reason(now=99, repairs=0) is None
    assert session.stop_reason(now=100, repairs=0) == 'remediation wall-clock budget exhausted'
    session.model_calls = 3
    assert session.stop_reason(now=90, repairs=0) == 'model-call budget exhausted'


def valid_prepared_manifest():
    """Supply a complete serialized bundle identity for boundary validation tests."""
    return {
        'schema_version': 1, 'source': 'demo', 'version': '1.0-1',
        'dsc': 'demo_1.0-1.dsc', 'dsc_sha256': 'a' * 64,
        'catalog_entry_sha256': 'b' * 64, 'binaries': ['python3-demo'],
        'build_depends': {'Build-Depends': 'debhelper-compat (= 13)'},
        'binary_packages': [{'package': 'python3-demo'}],
        'autopkgtests': [{'tests': 'smoke', 'depends': '@'}],
        'source_artifacts': [{'file': 'demo_1.0-1.dsc', 'sha256': 'a' * 64}],
        'pins': {'upstream_sha': 'c' * 40, 'packaging_sha': None},
        'ci': {'run_id': '7', 'run_attempt': '1'},
        'packaging_source': {'role': 'uca-ppa-source', 'url': 'https://example.invalid/demo.dsc'},
    }


@pytest.mark.parametrize(('key', 'value'), [
    ('binaries', 'python3-demo'),
    ('binaries', [None]),
    ('build_depends', {'Build-Depends': None}),
    ('binary_packages', [{'package': 'python3-demo', 'depends': ['python3-other']}]),
    ('autopkgtests', [{'tests': 'smoke', 'depends': 1}]),
    ('testsuite_triggers', None),
    ('source_artifacts', []),
    ('source_artifacts', [{'file': '../escape.tar.gz', 'sha256': 'a' * 64}]),
    ('source_artifacts', [{'file': 'dir\\escape.tar.gz', 'sha256': 'a' * 64}]),
    ('source_artifacts', [{'file': 'demo.tar.gz', 'sha256': 'bad'}]),
    ('pins', {'upstream_sha': []}),
    ('ci', {'run_id': 7, 'run_attempt': '1'}),
    ('packaging_source', []),
])
def test_prepared_manifest_rejects_malformed_nested_metadata(key, value):
    """Reject incompatible JSON shapes before dependency or filesystem operations."""
    from packagetest.contracts import prepared_manifest

    manifest = valid_prepared_manifest()
    manifest[key] = value
    with pytest.raises(ValueError):
        prepared_manifest(manifest)


def test_prepared_manifest_rejects_duplicate_component_names():
    """A duplicate inventory entry must not disappear inside a set comparison."""
    from packagetest.contracts import prepared_manifest

    manifest = valid_prepared_manifest()
    manifest['source_artifacts'] *= 2
    with pytest.raises(ValueError, match='duplicate'):
        prepared_manifest(manifest)


def test_prepared_manifest_detaches_nested_inputs_and_preserves_nullable_pins():
    """Normalize optional declarations without sharing caller-owned collections."""
    from packagetest.contracts import prepared_manifest

    original = valid_prepared_manifest()
    manifest = prepared_manifest(original)
    original['binary_packages'][0]['depends'] = 'changed'
    original['binaries'].clear()
    assert manifest['binaries'] == ['python3-demo']
    assert manifest['binary_packages'][0]['depends'] == ''
    assert manifest['autopkgtests'][0]['depends'] == '@'
    assert manifest['pins']['packaging_sha'] is None
