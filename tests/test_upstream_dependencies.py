from packagetest.upstream_dependencies import (
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


def test_raw_urls_are_bound_to_the_frozen_commit():
    sha = 'a' * 40
    assert raw_url('https://opendev.org/openstack/ironic', sha, 'requirements.txt') == (
        f'https://opendev.org/openstack/ironic/raw/commit/{sha}/requirements.txt')
    assert raw_url('https://github.com/example/project.git', sha, 'pyproject.toml') == (
        f'https://raw.githubusercontent.com/example/project/{sha}/pyproject.toml')
