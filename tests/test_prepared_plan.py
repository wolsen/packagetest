import hashlib
import json
from pathlib import Path
import sys

import pytest

from packagetest.prepared_plan import (apply_prepared_metadata, exact_dependencies,
                                       relation_groups, stable_digest)


def manifest(source, version, binaries, *, build='', tests=None, binary_depends=''):
    return {
        'source': source, 'version': version, 'binaries': binaries,
        'build_depends': {'Build-Depends': build, 'Build-Depends-Indep': '',
                          'Build-Depends-Arch': ''},
        'autopkgtests': tests or [], 'testsuite_triggers': '',
        'binary_packages': [
            {'package': binary, 'depends': binary_depends, 'pre_depends': ''}
            for binary in binaries],
    }


def test_relations_preserve_alternatives_and_versions():
    groups = relation_groups('python3-a (>= 2.0) | python3-b, debhelper-compat (= 13)')
    assert [[item['package'] for item in group] for group in groups] == [
        ['python3-a', 'python3-b'], ['debhelper-compat']]
    assert groups[0][0]['operator'] == '>='
    assert groups[1][0]['version'] == '13'


def test_debian_substvar_versions_do_not_reach_dpkg(monkeypatch):
    relation = relation_groups('python3-demo (= ${binary:Version})')[0][0]

    def unexpected(*args, **kwargs):
        raise AssertionError('dpkg must not compare an unresolved Debian substvar')

    monkeypatch.setattr('packagetest.prepared_plan.subprocess.run', unexpected)
    from packagetest.prepared_plan import satisfies
    assert satisfies('2.0-1', relation)


def test_exact_graph_uses_build_test_and_runtime_candidate_providers():
    catalog = {'packages': [
        {'source': 'a', 'archive_version': '1.0-1', 'binaries': ['python3-a']},
        {'source': 'b', 'archive_version': '1.0-1', 'binaries': ['python3-b']},
        {'source': 'c', 'archive_version': '1.0-1', 'binaries': ['python3-c']},
    ], 'archive_python_packages': [
        {'source': 'python-external', 'binary': 'python3-external', 'version': '4.0'},
    ]}
    manifests = {
        'a': manifest('a', '2.0-1', ['python3-a']),
        'b': manifest('b', '2.0-1', ['python3-b'],
                      build='debhelper-compat (= 13), python3-a (>= 2.0), python3-external',
                      tests=[{'tests': 'smoke', 'depends': 'python3-c'}]),
        'c': manifest('c', '2.0-1', ['python3-c'], binary_depends='python3-a'),
    }

    graph = exact_dependencies(catalog, manifests)
    dependencies, external = graph.dependencies, graph.external_decisions
    errors, reasons = graph.errors, graph.reasons

    assert dependencies == {'a': [], 'b': ['a', 'c'], 'c': ['a']}
    assert errors == []
    assert any(item['decision'] == 'ubuntu-archive' and
               item['binary'] == 'python3-external' for item in external)
    assert any(item['decision'] == 'system-archive-resolution' and
               'debhelper-compat' in item['binaries'] for item in external)
    assert all(isinstance(item.get('binaries', []), list) for item in external)
    json.dumps(external)
    assert {item['phase'] for item in reasons[('b', 'a')]} == {'build'}
    assert {item['phase'] for item in reasons[('b', 'c')]} == {'test'}
    assert {item['phase'] for item in reasons[('c', 'a')]} == {'runtime'}


def test_incompatible_selected_candidate_is_a_planning_error_without_archive_fallback():
    catalog = {'packages': [
        {'source': 'a', 'archive_version': '1.0-1', 'binaries': ['python3-a']},
        {'source': 'b', 'archive_version': '1.0-1', 'binaries': ['python3-b']},
    ]}
    manifests = {
        'a': manifest('a', '2.0-1', ['python3-a']),
        'b': manifest('b', '2.0-1', ['python3-b'], build='python3-a (>= 3.0)'),
    }
    graph = exact_dependencies(catalog, manifests)
    dependencies, external, errors = graph.dependencies, graph.external_decisions, graph.errors
    assert dependencies['b'] == []
    assert external == []
    assert errors[0]['candidate_sources'] == ['a']


def test_duplicate_prepared_binary_provider_is_rejected():
    catalog = {'packages': [
        {'source': 'a', 'archive_version': '1.0-1', 'binaries': ['python3-a']},
        {'source': 'b', 'archive_version': '1.0-1', 'binaries': ['python3-b']},
    ]}
    manifests = {
        'a': manifest('a', '2.0-1', ['python3-shared']),
        'b': manifest('b', '2.0-1', ['python3-shared']),
    }
    with pytest.raises(ValueError, match='Ambiguous prepared binary provider'):
        exact_dependencies(catalog, manifests)


def test_prepared_metadata_records_effective_ppa_source_and_clears_debian_warning():
    warning = 'Debian packaging is being used as the final fallback.'
    catalog = {'packages': [{
        'source': 'demo', 'binaries': ['python3-demo'],
        'packaging_source_role': 'ubuntu-importer',
        'packaging_fallback_warning': warning,
    }]}
    prepared = manifest('demo', '2.0-1', ['python3-demo'])
    prepared.update(dsc='demo_2.0-1.dsc', dsc_sha256='a' * 64,
                    catalog_entry_sha256='b' * 64)
    prepared['packaging_source'] = {
        'kind': 'archive', 'role': 'uca-ppa-source',
        'url': ('https://ppa.launchpadcontent.net/ubuntu-cloud-archive/'
                'hibiscus-staging/ubuntu/pool/main/d/demo/demo_1.0-1.dsc'),
    }

    result = apply_prepared_metadata(catalog, {'demo': prepared})['packages'][0]

    assert result['planned_packaging_source_role'] == 'ubuntu-importer'
    assert result['packaging_source_role'] == 'uca-ppa-source'
    assert result['effective_packaging_source']['kind'] == 'archive'
    assert 'packaging_fallback_warning' not in result


def write_prepared(root, entry, catalog, *, version, binaries, build=''):
    directory = root / f"prepared-source-{entry['source']}"
    source_dir = directory / 'source'
    source_dir.mkdir(parents=True)
    tarball = source_dir / f"{entry['source']}_{version}.orig.tar.gz"
    tarball.write_bytes(entry['source'].encode())
    tar_digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    dsc = source_dir / f"{entry['source']}_{version}.dsc"
    dsc.write_text(
        f"Source: {entry['source']}\nVersion: {version}\nBinary: {', '.join(binaries)}\n"
        f"Checksums-Sha256:\n {tar_digest} {tarball.stat().st_size} {tarball.name}\n")
    dsc_digest = hashlib.sha256(dsc.read_bytes()).hexdigest()
    value = manifest(entry['source'], version, binaries, build=build)
    value.update({
        'schema_version': 1, 'dsc': dsc.name, 'dsc_sha256': dsc_digest,
        'source_artifacts': [
            {'file': dsc.name, 'sha256': dsc_digest},
            {'file': tarball.name, 'sha256': tar_digest},
        ],
        'catalog_entry_sha256': stable_digest(entry),
        'pins': {'upstream_sha': entry['upstream_sha'],
                 'packaging_sha': entry['packaging_sha']},
        'ci': catalog['ci'], 'testsuite': '',
    })
    (directory / 'prepared-source.json').write_text(json.dumps(value))


def test_final_planner_builds_levels_from_prepared_sources(tmp_path, monkeypatch):
    entries = [
        {'source': 'a', 'archive_version': '1.0-1', 'binaries': ['python3-old-a'],
         'upstream_sha': 'a' * 40, 'packaging_sha': 'b' * 40,
         'packaging_source_kind': 'git'},
        {'source': 'b', 'archive_version': '1.0-1', 'binaries': ['python3-b'],
         'upstream_sha': 'c' * 40, 'packaging_sha': 'd' * 40,
         'packaging_source_kind': 'git'},
    ]
    catalog = {'schema_version': 1, 'series': '2026.2', 'suite': 'resolute',
               'ci': {'run_id': '7', 'run_attempt': '1'},
               'requested_sources': ['b'], 'packages': entries}
    prepared = tmp_path / 'prepared'
    write_prepared(prepared, entries[0], catalog, version='2.0-1', binaries=['python3-a'])
    write_prepared(prepared, entries[1], catalog, version='2.0-1', binaries=['python3-b'],
                   build='python3-a (>= 2.0)')
    catalog_path = tmp_path / 'catalog.json'
    catalog_path.write_text(json.dumps(catalog))
    constraints = tmp_path / 'constraints.json'
    constraints.write_text('[]')
    output = tmp_path / 'final'
    from packagetest import final_plan as module
    path = Path('scripts/finalize-nightly-plan.py')
    monkeypatch.setattr(sys, 'argv', [str(path), '--catalog', str(catalog_path),
                        '--prepared-inputs', str(prepared), '--output', str(output),
                        '--candidate-dependencies', str(constraints)])

    assert module.main() == 0
    plan = json.loads((output / 'plan.json').read_text())
    final_catalog = json.loads((output / 'catalog.json').read_text())
    final_entries = {entry['source']: entry for entry in final_catalog['packages']}
    assert plan['waves'] == [['a'], ['b']]
    assert plan['dependency_edges'][0]['reasons'][0]['binary'] == 'python3-a'
    assert final_entries['a']['archive_binaries'] == ['python3-old-a']
    assert final_entries['a']['binaries'] == ['python3-a']


@pytest.mark.parametrize(('expression', 'profiles', 'expected'), [
    ('python3-a [arm64]', (), False),
    ('python3-a [amd64]', (), True),
    ('python3-a [linux-any]', (), True),
    ('python3-a [!amd64]', (), False),
    ('python3-a <cross>', (), False),
    ('python3-a <cross>', ('cross',), True),
    ('python3-a <!nocheck>', ('nocheck',), False),
    ('python3-a <cross nocheck>', ('cross',), False),
    ('python3-a <cross> <nocheck>', ('nocheck',), True),
])
def test_exact_graph_respects_target_restrictions(expression, profiles, expected):
    from packagetest.relations import BuildContext
    manifests = {'a': manifest('a', '1', ['python3-a']),
                 'b': manifest('b', '1', ['python3-b'], build=expression)}
    graph = exact_dependencies(
        {'packages': []}, manifests, context=BuildContext('amd64', frozenset(profiles)))
    assert graph.dependencies['b'] == (['a'] if expected else [])
    assert not graph.errors


def test_restricted_alternative_is_removed_before_selecting_provider():
    assert relation_groups('python3-a [arm64] | python3-b')[0][0]['package'] == 'python3-b'


@pytest.mark.parametrize('expression', ['python3-a garbage', 'python3-a [amd64 !arm64]',
                                        'python3-a < >', 'python3-a (invalid)'])
def test_unknown_relation_syntax_is_rejected(expression):
    with pytest.raises(ValueError):
        relation_groups(expression)


def test_deferred_substitutions_are_not_mistaken_for_packages():
    assert relation_groups('@, @builddeps@, ${misc:Depends}') == []
