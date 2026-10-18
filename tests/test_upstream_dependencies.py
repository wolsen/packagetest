from packagetest.upstream_dependencies import (
    archive_requirement_decision,
    debian_upstream_version,
    distribution_archive_index,
    distribution_source_index,
    map_requirements,
    parse_pyproject,
    parse_requirement_file,
    raw_url,
)


def test_requirement_files_capture_runtime_and_test_dependencies():
    runtime = parse_requirement_file(
        '# comment\nsushy>=5.12.0 # Apache-2.0\noslo.versionedobjects>=3.5.0; python_version >= "3.10"\n',
        filename='requirements.txt', kind='runtime')
    tests = parse_requirement_file('stestr>=2.0\n-r common.txt\n',
                                   filename='test-requirements.txt', kind='test')

    assert [item['distribution'] for item in runtime] == ['sushy', 'oslo-versionedobjects']
    assert runtime[0]['requirement'] == 'sushy>=5.12.0 # Apache-2.0'
    assert tests[0]['kind'] == 'test'
    assert tests[1]['distribution'] is None


def test_pyproject_captures_project_build_and_test_groups():
    records = parse_pyproject('''
[build-system]
requires = ["setuptools>=68", "pbr>=6"]
[project]
dependencies = ["sushy>=5.12"]
[project.optional-dependencies]
test = ["stestr>=4"]
docs = ["sphinx>=7"]
[dependency-groups]
testing = ["fixtures>=4"]
''')

    assert {(item['distribution'], item['kind']) for item in records} == {
        ('setuptools', 'build-system'), ('pbr', 'build-system'),
        ('sushy', 'runtime'), ('stestr', 'test'), ('fixtures', 'test')}
    assert 'sphinx' not in {item['distribution'] for item in records}


def test_distribution_mapping_uses_binary_source_and_python_names():
    packages = [
        {'source': 'ironic', 'deliverable': 'ironic', 'binaries': ['python3-ironic']},
        {'source': 'python-sushy', 'deliverable': 'sushy', 'binaries': ['python3-sushy']},
        {'source': 'python-oslo.versionedobjects', 'deliverable': 'oslo.versionedobjects',
         'binaries': ['python3-oslo.versionedobjects']},
        {'source': 'python-yaml', 'deliverable': 'yaml', 'binaries': ['python3-yaml']},
    ]
    index = distribution_source_index(packages)
    records = parse_requirement_file('sushy>=5.12\noslo.versionedobjects>=3\nPyYAML>=6\n',
                                     filename='requirements.txt', kind='runtime')
    mapped, dependencies = map_requirements(records, index, 'ironic')

    assert dependencies == ['python-oslo.versionedobjects', 'python-sushy', 'python-yaml']
    assert {item['distribution']: item.get('source') for item in mapped} == {
        'sushy': 'python-sushy',
        'oslo-versionedobjects': 'python-oslo.versionedobjects',
        'pyyaml': 'python-yaml',
    }


def test_external_test_requirements_are_checked_against_ubuntu_archive():
    archive = distribution_archive_index([
        {'distribution': 'wsgi-intercept', 'binary': 'python3-wsgi-intercept',
         'source': 'python-wsgi-intercept', 'version': '1.13.1-1'},
        {'distribution': 'gabbi', 'binary': 'python3-gabbi',
         'source': 'python-gabbi', 'version': '3.0.0-1'},
    ])
    records = parse_requirement_file(
        'wsgi-intercept>=1.7\ngabbi>=1.35\nnot-in-ubuntu>=1\n',
        filename='test-requirements.txt', kind='test')
    mapped, dependencies = map_requirements(records, {}, 'watcher', {}, archive)

    assert dependencies == []
    assert mapped[0]['archive_binary'] == 'python3-wsgi-intercept'
    assert mapped[0]['archive_decision'] == 'satisfied'
    assert mapped[1]['archive_binary'] == 'python3-gabbi'
    assert mapped[2]['archive_decision'] == 'unmapped'


def test_source_does_not_treat_its_own_binary_as_an_external_dependency():
    records = parse_requirement_file('demo>=1\n', filename='requirements.txt', kind='runtime')
    archive = distribution_archive_index([{
        'distribution': 'demo', 'binary': 'python3-demo',
        'source': 'python-demo', 'version': '2.0-1',
    }])
    mapped, dependencies = map_requirements(
        records, {'demo': 'python-demo'}, 'python-demo',
        {'python-demo': {'archive_version': '2.0-1'}}, archive)
    assert dependencies == []
    assert mapped[0]['source'] == 'python-demo'
    assert 'archive_binary' not in mapped[0]


def test_ubuntu_archive_versions_are_checked_against_pep508_constraints():
    assert debian_upstream_version('2:7.0.3-0ubuntu1') == '7.0.3'
    assert debian_upstream_version('5.12.0~rc1-0ubuntu1') == '5.12.0rc1'
    assert archive_requirement_decision('pbr>=6.0.0', '2:7.0.3-0ubuntu1')[0] is True
    assert archive_requirement_decision('sushy>=5.12.0', '5.9.0-0ubuntu1')[0] is False
    assert archive_requirement_decision('sample!=2.0,>=1.0', '2.0-0ubuntu1')[0] is False
    assert archive_requirement_decision('sample~=2.1', '2.4.0-0ubuntu1')[0] is True


def test_only_unsatisfied_or_unknown_requirements_become_candidate_dependencies():
    records = parse_requirement_file('pbr>=6\nsushy>=5.12\nunknown @ https://example.invalid/a.whl\n',
                                     filename='requirements.txt', kind='runtime')
    packages = {
        'python-pbr': {'archive_version': '7.0.3-0ubuntu1'},
        'python-sushy': {'archive_version': '5.9.0-0ubuntu1'},
        'python-unknown': {'archive_version': '1.0-0ubuntu1'},
    }
    index = {'pbr': 'python-pbr', 'sushy': 'python-sushy', 'unknown': 'python-unknown'}
    mapped, dependencies = map_requirements(records, index, 'ironic', packages)

    assert dependencies == ['python-sushy', 'python-unknown']
    decisions = {item['distribution']: item['archive_decision'] for item in mapped}
    assert decisions == {'pbr': 'satisfied', 'sushy': 'insufficient',
                         'unknown': 'unknown'}


def test_raw_urls_are_bound_to_the_frozen_commit():
    sha = 'a' * 40
    assert raw_url('https://opendev.org/openstack/ironic', sha, 'requirements.txt') == (
        f'https://opendev.org/openstack/ironic/raw/commit/{sha}/requirements.txt')
    assert raw_url('https://github.com/example/project.git', sha, 'pyproject.toml') == (
        f'https://raw.githubusercontent.com/example/project/{sha}/pyproject.toml')


def test_optional_metadata_retries_transient_failure_before_absence(monkeypatch):
    """A later 404 is definitive absence even after a retryable HTTP failure."""
    from unittest.mock import Mock
    from urllib.error import HTTPError

    from packagetest import upstream_dependencies as metadata

    url = 'https://example.invalid/requirements.txt'
    request = Mock(side_effect=[HTTPError(url, 503, 'Unavailable', {}, None),
                                HTTPError(url, 404, 'Absent', {}, None)])
    sleep = Mock()
    monkeypatch.setattr(metadata, 'urlopen', request)
    monkeypatch.setattr(metadata.time, 'sleep', sleep)

    assert metadata.fetch_optional(url) is None
    assert request.call_count == 2
    sleep.assert_called_once_with(1)


def test_optional_metadata_exhaustion_preserves_last_failure(monkeypatch):
    """Exhausted retries retain the last diagnostic and exponential backoff."""
    from unittest.mock import Mock
    from urllib.error import URLError

    import pytest

    from packagetest import upstream_dependencies as metadata

    request = Mock(side_effect=[TimeoutError('first'), URLError('second'),
                                UnicodeDecodeError('utf8', b'\xff', 0, 1, 'last')])
    sleep = Mock()
    monkeypatch.setattr(metadata, 'urlopen', request)
    monkeypatch.setattr(metadata.time, 'sleep', sleep)

    with pytest.raises(metadata.MetadataFetchError, match='last'):
        metadata.fetch_optional('https://example.invalid/requirements.txt')

    assert request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


def test_provider_ambiguity_is_not_hidden_by_distribution_aliases():
    """Conflicting providers stay unresolved through both source and binary indexes."""
    packages = [
        {'source': 'python-yaml', 'binaries': ['python3-yaml']},
        {'source': 'other', 'deliverable': 'pyyaml', 'binaries': []},
    ]
    assert 'pyyaml' not in distribution_source_index(packages)
    archive = distribution_archive_index([
        {'distribution': 'yaml', 'binary': 'python3-yaml',
         'source': 'python-yaml', 'version': '1'},
        {'distribution': 'pyyaml', 'binary': 'other', 'source': 'other', 'version': '1'},
    ])
    assert 'pyyaml' not in archive


def test_pyproject_rejects_a_string_instead_of_a_requirement_array():
    """Malformed TOML must not turn one requirement into per-character records."""
    import pytest

    with pytest.raises(ValueError, match='must be a list'):
        parse_pyproject('[project]\ndependencies = "sushy>=5"\n')


def test_dependency_group_includes_are_not_guessed_as_distribution_names():
    """Structured group references remain unresolved while literal requirements survive."""
    records = parse_pyproject('''
[dependency-groups]
test = [{include-group = "common"}, "stestr>=4"]
''')
    assert [record['distribution'] for record in records] == ['stestr']


def test_metadata_fetch_rejects_empty_retry_budget_before_network_access(monkeypatch):
    """Invalid fetch settings produce a clear error rather than an unbound diagnostic."""
    import pytest

    from packagetest import upstream_dependencies as metadata

    def unexpected(*args, **kwargs):
        """Fail if invalid settings reach the network boundary."""
        raise AssertionError('invalid fetch settings must not contact upstream')

    monkeypatch.setattr(metadata, 'urlopen', unexpected)
    for settings in ({'attempts': 0}, {'attempts': -1}, {'timeout': 0}):
        with pytest.raises(ValueError, match='must be positive'):
            metadata.fetch_optional('https://example.invalid/requirements.txt', **settings)


def test_inspection_selects_tox_test_extras_from_same_immutable_revision():
    from packagetest.upstream_dependencies import inspect_revision

    files = {
        'tox.ini': '[testenv]\nextras =\n cloud_api, locking # both required by tests\n[testenv:docs]\nextras = docs\n',
        'pyproject.toml': '''
[project.optional-dependencies]
cloud-api = ["kubernetes>=2.8.1; python_version >= '3.10'"]
locking = ["sherlock>=0.4.1"]
docs = ["sphinx>=7"]
test = ["stestr>=4"]
''',
    }
    seen = []

    def fetch(url):
        seen.append(url)
        return files.get(url.rsplit('/', 1)[-1])

    records, present = inspect_revision({
        'source': 'sample', 'upstream_repository': 'https://github.com/example/sample',
        'upstream_sha': 'a' * 40,
    }, fetch=fetch)
    assert all('/' + 'a' * 40 + '/' in url for url in seen)
    assert set(present) == {'tox.ini', 'pyproject.toml'}
    assert {r['distribution'] for r in records} == {'kubernetes', 'sherlock', 'stestr'}
    assert all(r['kind'] == 'test' and r['file'] == 'pyproject.toml' for r in records)
    assert records[0]['requirement'] == "kubernetes>=2.8.1; python_version >= '3.10'"
    assert records[0]['group'] == 'cloud-api'


def test_native_and_embedded_tox_test_extras_are_discovered():
    native = parse_pyproject('''
[tool.tox.env_run_base]
extras = ["cloud-api"]
[project.optional-dependencies]
cloud-api = ["kubernetes>=2.8.1", "sherlock>=0.4.1"]
docs = ["sphinx>=7"]
''')
    embedded = parse_pyproject('''
[tool.tox]
legacy_tox_ini = "[testenv]\\nextras = cloud-api\\n"
[project.optional-dependencies]
cloud-api = ["kubernetes>=2.8.1", "sherlock>=0.4.1"]
''')
    assert native == embedded
    assert [r['requirement'] for r in native] == ['kubernetes>=2.8.1', 'sherlock>=0.4.1']


def test_unresolved_tox_substitutions_are_reported_instead_of_guessed():
    import pytest

    from packagetest.upstream_dependencies import MetadataFetchError

    with pytest.raises(MetadataFetchError, match='statically resolve'):
        parse_pyproject('[tool.tox.env_run_base]\nextras = ["{env:TEST_EXTRAS}"]\n')
