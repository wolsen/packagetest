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
