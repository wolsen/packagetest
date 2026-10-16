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
