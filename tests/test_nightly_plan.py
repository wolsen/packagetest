import json
from types import SimpleNamespace

import pytest

from packagetest import nightly_plan as module


def catalog():
    return {'series': '2026.2',
            'packages': [{'source': 'a', 'build_dependencies': ['b']},
                         {'source': 'b', 'build_dependencies': ['a']},
                         {'source': 'c', 'build_dependencies': ['a']},
                         {'source': 'd', 'build_dependencies': []}]}


def test_cycles_are_explicit_bootstrap_not_silently_omitted():
    frozen, plan = module.plan_catalog(catalog())
    assert plan['waves'] == [['a', 'b', 'd'], ['c']]
    assert {(e['source'], e['dependency']) for e in plan['archive_bootstrap_edges']} == {('a', 'b'), ('b', 'a')}
    assert frozen['packages'][2]['run_dependencies'] == ['a']
    assert frozen['packages'][0]['archive_bootstrap_dependencies'] == ['b']


def test_pilot_exact_selection_reports_archive_dependencies():
    frozen, plan = module.plan_catalog(catalog(), ['c'])
    assert plan['sources'] == ['c']
    assert frozen['packages'][0]['archive_bootstrap_dependencies'] == ['a']
    assert plan['archive_bootstrap_edges'][0]['reason'] == 'outside explicitly selected pilot'


def test_wave_capacity_and_unknown_source_fail():
    with pytest.raises(ValueError, match='capacity'):
        module.plan_catalog(catalog(), max_waves=1)
    with pytest.raises(ValueError, match='Unknown'):
        module.plan_catalog(catalog(), ['missing'])


def test_resolution_failure_is_individual_and_never_branch_fallback(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError('network timed out')
    monkeypatch.setattr(module.subprocess, 'run', fail)
    result = module.freeze({'source': 'a', 'upstream_repository': 'https://example.test/a', 'upstream_ref': 'stable/2026.2'})
    assert result['upstream_sha'] is None
    assert result['upstream_resolution_error'] == 'network timed out'
    assert result['upstream_ref'] == 'stable/2026.2'


def test_freeze_pins_launchpad_packaging_and_related_source_branches(monkeypatch):
    upstream_sha = '1' * 40
    packaging_sha = '2' * 40
    packaging_upstream_sha = '3' * 40
    pristine_sha = '4' * 40

    def run(command, **kwargs):
        if command[3] == 'https://opendev.org/openstack/demo':
            return SimpleNamespace(stdout=f'{upstream_sha}\trefs/heads/stable/2026.2\n',
                                   stderr='', returncode=0)
        return SimpleNamespace(stdout=(
            f'{packaging_sha}\trefs/heads/master\n'
            f'{packaging_upstream_sha}\trefs/heads/upstream-hibiscus\n'
            f'{pristine_sha}\trefs/heads/pristine-tar\n'), stderr='', returncode=0)

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze({
        'source': 'demo',
        'upstream_repository': 'https://opendev.org/openstack/demo',
        'upstream_ref': 'stable/2026.2',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo/+git/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'packaging_upstream_branch_candidates': ['upstream-hibiscus', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
    })

    assert entry['upstream_sha'] == upstream_sha
    assert entry['packaging_source_kind'] == 'git'
    assert entry['packaging_branch'] == 'master'
    assert entry['packaging_sha'] == packaging_sha
    assert entry['packaging_upstream_branch'] == 'upstream-hibiscus'
    assert entry['packaging_upstream_sha'] == packaging_upstream_sha
    assert entry['packaging_pristine_tar_sha'] == pristine_sha


def test_packaging_resolution_uses_archive_vcs_when_launchpad_tree_is_absent(monkeypatch):
    revision = '5' * 40

    def run(command, **kwargs):
        if 'git.launchpad.net' in command[3]:
            return SimpleNamespace(stdout='', stderr='repository does not exist', returncode=128)
        return SimpleNamespace(stdout=f'{revision}\trefs/heads/debian/hibiscus\n',
                               stderr='', returncode=0)

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze_packaging({
        'source': 'demo',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo/+git/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'packaging_upstream_branch_candidates': ['upstream-hibiscus', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
        'archive_packaging_repository': 'https://salsa.example/openstack/demo.git',
        'archive_packaging_branch': 'debian/hibiscus',
    })

    assert entry['packaging_source_kind'] == 'git'
    assert entry['packaging_source_role'] == 'series-archive-vcs'
    assert entry['packaging_source_repository'] == 'https://salsa.example/openstack/demo.git'
    assert entry['packaging_branch'] == 'debian/hibiscus'
    assert entry['packaging_sha'] == revision
    assert 'Debian packaging is being used as the final fallback' in (
        entry['packaging_fallback_warning'])


def test_packaging_resolution_prefers_exact_uca_ppa_source_over_debian(monkeypatch):
    def run(command, **kwargs):
        repository = command[3]
        if 'launchpad.net' in repository:
            return SimpleNamespace(stdout='', stderr='repository does not exist', returncode=128)
        raise AssertionError(f'Debian VCS must not be queried: {repository}')

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze_packaging({
        'source': 'demo',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo/+git/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'ubuntu_importer_repository': 'https://git.launchpad.net/ubuntu/+source/demo',
        'ubuntu_importer_branch_candidates': ['ubuntu/resolute'],
        'archive_packaging_repository': 'https://salsa.debian.org/openstack-team/demo.git',
        'archive_packaging_series_branch': 'debian/hibiscus',
        'archive_source': {
            'url': ('https://ppa.launchpadcontent.net/ubuntu-cloud-archive/'
                    'hibiscus-staging/ubuntu/pool/main/d/demo/demo_1.0-1~cloud0.dsc'),
            'sha256': '0' * 64,
        },
    })

    assert entry['packaging_source_kind'] == 'archive'
    assert entry['packaging_source_role'] == 'uca-ppa-source'
    assert 'packaging_fallback_warning' not in entry


def test_packaging_resolution_uses_importer_before_series_archive_vcs(monkeypatch):
    revision = '6' * 40

    def run(command, **kwargs):
        repository = command[3]
        if '~ubuntu-openstack-dev' in repository:
            return SimpleNamespace(stdout='', stderr='repository does not exist', returncode=128)
        if repository == 'https://git.launchpad.net/ubuntu/+source/demo':
            return SimpleNamespace(
                stdout=f'{revision}\trefs/heads/ubuntu/resolute\n',
                stderr='', returncode=0)
        raise AssertionError(f'archive VCS should not be queried: {repository}')

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze_packaging({
        'source': 'demo',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo/+git/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'ubuntu_importer_repository':
            'https://git.launchpad.net/ubuntu/+source/demo',
        'ubuntu_importer_branch_candidates': ['ubuntu/resolute', 'ubuntu/resolute-devel'],
        'packaging_upstream_branch_candidates': ['upstream-hibiscus', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
        'archive_packaging_repository': 'https://salsa.example/openstack/demo.git',
        'archive_packaging_branch': 'debian/hibiscus',
        'archive_packaging_series_branch': 'debian/hibiscus',
    })

    assert entry['packaging_source_kind'] == 'git'
    assert entry['packaging_source_role'] == 'ubuntu-importer'
    assert entry['packaging_source_repository'] == (
        'https://git.launchpad.net/ubuntu/+source/demo')
    assert entry['packaging_branch'] == 'ubuntu/resolute'
    assert entry['packaging_sha'] == revision


def test_packaging_resolution_uses_series_archive_vcs_when_importer_is_absent(monkeypatch):
    revision = '7' * 40

    def run(command, **kwargs):
        repository = command[3]
        if '~ubuntu-openstack-dev' in repository:
            return SimpleNamespace(stdout='', stderr='repository does not exist', returncode=128)
        if repository == 'https://salsa.example/openstack/demo.git':
            requested = set(command[4:])
            if 'refs/heads/debian/hibiscus' in requested:
                return SimpleNamespace(
                    stdout=f'{revision}\trefs/heads/debian/hibiscus\n',
                    stderr='', returncode=0)
            raise AssertionError('generic archive VCS should not be queried')
        if repository == 'https://git.launchpad.net/ubuntu/+source/demo':
            return SimpleNamespace(stdout='', stderr='repository does not exist',
                                   returncode=128)
        raise AssertionError(repository)

    monkeypatch.setattr(module.subprocess, 'run', run)
    entry = module.freeze_packaging({
        'source': 'demo',
        'packaging_repository':
            'https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/demo/+git/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
        'ubuntu_importer_repository':
            'https://git.launchpad.net/ubuntu/+source/demo',
        'ubuntu_importer_branch_candidates': ['ubuntu/resolute', 'ubuntu/resolute-devel'],
        'packaging_upstream_branch_candidates': ['upstream-hibiscus', 'upstream'],
        'packaging_pristine_tar_branch': 'pristine-tar',
        'archive_packaging_repository': 'https://salsa.example/openstack/demo.git',
        'archive_packaging_branch': 'debian/unstable',
        'archive_packaging_series_branch': 'debian/hibiscus',
    })

    assert entry['packaging_source_role'] == 'series-archive-vcs'
    assert entry['packaging_branch'] == 'debian/hibiscus'


def test_mandatory_candidate_restores_one_direction_of_cycle():
    policy = [{'source': 'a', 'dependency': 'b', 'reason': 'new API'}]
    frozen, plan = module.plan_catalog(catalog(), candidate_dependencies=policy)
    assert plan['waves'] == [['b', 'd'], ['a'], ['c']]
    assert {(e['source'], e['dependency']) for e in plan['archive_bootstrap_edges']} == {('b', 'a')}
    assert frozen['packages'][0]['run_dependencies'] == ['b']
    assert frozen['packages'][0]['required_candidate_dependencies'] == {'b': 'new API'}
    with pytest.raises(ValueError, match='include it'):
        module.plan_catalog(catalog(), ['a'], candidate_dependencies=policy)


def test_mandatory_candidate_cycle_cannot_silently_bootstrap():
    policy = [{'source': 'a', 'dependency': 'b', 'reason': 'new API'},
              {'source': 'b', 'dependency': 'a', 'reason': 'new API'}]
    with pytest.raises(ValueError, match='Unresolved dependency cycle'):
        module.plan_catalog(catalog(), candidate_dependencies=policy)


def test_cycle_uses_candidate_already_available_in_earlier_wave():
    data = {'packages': [{'source': 'a', 'build_dependencies': ['b', 'd']},
                         {'source': 'b', 'build_dependencies': ['a']},
                         {'source': 'd', 'build_dependencies': []}]}
    frozen, plan = module.plan_catalog(data)
    assert plan['waves'] == [['b', 'd'], ['a']]
    assert frozen['packages'][0]['run_dependencies'] == ['b', 'd']
    assert [(e['source'], e['dependency']) for e in plan['archive_bootstrap_edges']] == [('b', 'a')]


def test_plan_summary_lists_every_dependency_level_and_candidate_rule():
    policy = [{'source': 'a', 'dependency': 'b', 'reason': 'requires new | API'}]
    frozen, plan = module.plan_catalog(catalog(), candidate_dependencies=policy)
    plan['resolution_failures'] = []
    summary = module.render_plan_summary(plan, frozen)

    assert '# OpenStack 2026.2 snapshot build plan' in summary
    assert '4 source packages across 3 dependency levels' in summary
    assert '| 1 | 2 | `b`, `d` |' in summary
    assert '| 2 | 1 | `a` |' in summary
    assert '| 3 | 1 | `c` |' in summary
    assert '| `a` | `b` | requires new \\| API |' in summary
    assert 'All selected source references resolved to immutable commit SHAs.' in summary


def test_dynamic_matrix_contains_only_real_dependency_levels():
    matrix = module.dependency_level_matrix([['a', 'b'], ['c'], []])
    assert matrix == {'include': [
        {'level': 1, 'packages': '["a","b"]'},
        {'level': 2, 'packages': '["c"]'},
    ]}
    assert '__empty__' not in json.dumps(matrix)


def test_discovery_summary_defers_dependency_levels_until_preparation():
    data = {'series': '2027.1', 'series_status': 'development', 'packages': [
        {'source': 'a', 'selection_reasons': ['requested'], 'upstream_sha': '1' * 40,
         'upstream_ref': 'master', 'branch_policy': 'release-metadata-development-branch',
         'packaging_sha': '2' * 40},
    ]}
    summary = module.render_discovery_summary(data, ['a'], ['a'])
    assert '# OpenStack 2027.1 snapshot source discovery' in summary
    assert 'Series status: `development`' in summary
    assert 'selected and pinned for parallel source preparation' in summary
    assert 'Dependency levels will be computed after' in summary
    assert ('| `a` | requested | No target-archive package | '
            '`master` (release-metadata-development-branch) |') in summary


def test_discovery_summary_highlights_debian_packaging_fallback():
    data = {'series': '2026.2', 'packages': [{
        'source': 'a', 'selection_reasons': ['requested'],
        'upstream_ref': 'stable/2026.2', 'upstream_sha': '1' * 40,
        'packaging_sha': '2' * 40,
        'packaging_fallback_warning': module.DEBIAN_PACKAGING_FALLBACK_WARNING,
    }]}

    summary = module.render_discovery_summary(data, ['a'], ['a'])

    assert '## Packaging fallback warnings' in summary
    assert '**WARNING — `a`:**' in summary
    assert 'Debian packaging is being used as the final fallback' in summary


def test_upstream_metadata_expands_requested_roots_before_planning():
    data = {'packages': [
        {'source': 'ironic', 'deliverable': 'ironic', 'binaries': ['python3-ironic'],
         'build_dependencies': [], 'upstream_repository': 'https://opendev.org/openstack/ironic',
         'upstream_ref': 'master'},
        {'source': 'python-sushy', 'deliverable': 'sushy', 'binaries': ['python3-sushy'],
         'archive_version': '5.9.0-0ubuntu1', 'build_dependencies': [],
         'upstream_repository': 'https://opendev.org/openstack/sushy',
         'upstream_ref': 'stable/2026.2'},
        {'source': 'python-pbr', 'deliverable': 'pbr', 'binaries': ['python3-pbr'],
         'archive_version': '7.0.3-2', 'build_dependencies': [],
         'upstream_repository': 'https://opendev.org/openstack/pbr',
         'upstream_ref': 'master'},
        {'source': 'unrelated', 'deliverable': 'unrelated', 'binaries': ['python3-unrelated'],
         'build_dependencies': [], 'upstream_repository': 'https://opendev.org/openstack/unrelated',
         'upstream_ref': 'master'},
    ]}

    def freeze(entry):
        entry['upstream_sha'] = 'a' * 40
        return entry

    def inspect(entry):
        if entry['source'] == 'ironic':
            return ([{'distribution': 'sushy', 'requirement': 'sushy>=5.12.0',
                      'kind': 'runtime', 'file': 'requirements.txt', 'line': 1},
                     {'distribution': 'pbr', 'requirement': 'pbr>=6.0.0',
                      'kind': 'runtime', 'file': 'requirements.txt', 'line': 2}],
                    ['requirements.txt'])
        return [], ['pyproject.toml']

    enriched, selected, roots, errors = module.resolve_upstream_dependency_closure(
        data, ['ironic'], freeze_entry=freeze, inspect_entry=inspect)
    frozen, plan = module.plan_catalog(enriched, selected)
    entries = {entry['source']: entry for entry in frozen['packages']}

    assert roots == ['ironic']
    assert selected == ['ironic', 'python-sushy']
    assert errors == []
    assert plan['waves'] == [['python-sushy'], ['ironic']]
    assert entries['ironic']['run_dependencies'] == ['python-sushy']
    assert entries['python-sushy']['selection_reasons'] == ['upstream dependency of ironic']
    assert entries['ironic']['upstream_dependency_requirements'][0]['source'] == 'python-sushy'
    assert entries['ironic']['upstream_dependency_requirements'][1]['archive_decision'] == 'satisfied'


def test_upstream_external_test_dependency_is_annotated_without_dag_node():
    data = {
        'archive_python_packages': [{
            'distribution': 'wsgi-intercept', 'binary': 'python3-wsgi-intercept',
            'source': 'python-wsgi-intercept', 'version': '1.13.1-1',
        }],
        'packages': [{
            'source': 'watcher', 'deliverable': 'watcher', 'binaries': ['python3-watcher'],
            'build_dependencies': [], 'upstream_repository': 'https://opendev.org/openstack/watcher',
            'upstream_ref': 'master',
        }],
    }

    def freeze(entry):
        entry['upstream_sha'] = 'a' * 40
        return entry

    def inspect(entry):
        return ([{'distribution': 'wsgi-intercept', 'requirement': 'wsgi-intercept>=1.7',
                  'kind': 'test', 'file': 'test-requirements.txt', 'line': 1}],
                ['test-requirements.txt'])

    enriched, selected, _, errors = module.resolve_upstream_dependency_closure(
        data, ['watcher'], freeze_entry=freeze, inspect_entry=inspect)
    record = enriched['packages'][0]['upstream_dependency_requirements'][0]
    assert selected == ['watcher']
    assert errors == []
    assert record['archive_binary'] == 'python3-wsgi-intercept'
    assert record['archive_decision'] == 'satisfied'
    assert enriched['packages'][0]['planned_archive_dependency_additions'] == [{
        'binary': 'python3-wsgi-intercept', 'source': 'python-wsgi-intercept',
        'archive_version': '1.13.1-1', 'kind': 'test',
        'requirement': 'wsgi-intercept>=1.7', 'file': 'test-requirements.txt', 'line': 1,
    }]


def test_scheduler_preserves_complete_plan_and_input():
    data = catalog()
    original = json.loads(json.dumps(data))
    frozen, plan = module.plan_catalog(
        data,
        candidate_dependencies=[
            {"source": "a", "dependency": "b", "reason": "new API"},
        ],
    )
    assert data == original
    assert plan == {
        "sources": ["a", "b", "c", "d"],
        "waves": [["b", "d"], ["a"], ["c"]],
        "archive_bootstrap_edges": [
            {
                "source": "b",
                "dependency": "a",
                "reason": "dependency cycle bootstrap",
                "component": ["a", "b"],
            }
        ],
        "cycle_components": [["a", "b"]],
    }
    assert frozen == {
        "series": "2026.2",
        "packages": [
            {
                "source": "a",
                "build_dependencies": ["b"],
                "required_candidate_dependencies": {"b": "new API"},
                "run_dependencies": ["b"],
                "archive_bootstrap_dependencies": [],
                "wave": 1,
            },
            {
                "source": "b",
                "build_dependencies": ["a"],
                "run_dependencies": [],
                "archive_bootstrap_dependencies": ["a"],
                "wave": 0,
            },
            {
                "source": "c",
                "build_dependencies": ["a"],
                "run_dependencies": ["a"],
                "archive_bootstrap_dependencies": [],
                "wave": 2,
            },
            {
                "source": "d",
                "build_dependencies": [],
                "run_dependencies": [],
                "archive_bootstrap_dependencies": [],
                "wave": 0,
            },
        ],
    }
    reversed_data = {**data, "packages": list(reversed(data["packages"]))}
    assert module.plan_catalog(reversed_data) == module.plan_catalog(data)


def test_discovery_pins_once_and_isolates_metadata_failure():
    data = {
        "packages": [
            {
                "source": name,
                "deliverable": name,
                "binaries": [f"python3-{name}"],
                "build_dependencies": [],
            }
            for name in ("a", "b", "c")
        ]
    }
    original = json.loads(json.dumps(data))
    frozen_sources = []
    inspected_sources = []

    def freeze(entry):
        frozen_sources.append(entry["source"])
        entry["upstream_sha"] = "a" * 40
        return entry

    def inspect(entry):
        source = entry["source"]
        inspected_sources.append(source)
        if source == "c":
            raise ValueError("metadata unavailable")
        return (
            [
                {
                    "distribution": "b" if source == "a" else "a",
                    "requirement": "b" if source == "a" else "a",
                    "kind": "runtime",
                    "file": "requirements.txt",
                    "line": 1,
                }
            ],
            ["requirements.txt"],
        )

    enriched, selected, roots, errors = module.resolve_upstream_dependency_closure(
        data, ["a", "c"], freeze_entry=freeze, inspect_entry=inspect
    )
    assert data == original
    assert sorted(frozen_sources) == ["a", "b", "c"]
    assert sorted(inspected_sources) == ["a", "b", "c"]
    assert selected == ["a", "b", "c"]
    assert roots == ["a", "c"]
    assert errors == [{"source": "c", "error": "metadata unavailable"}]
    assert enriched["packages"][1]["selection_reasons"] == ["upstream dependency of a"]


@pytest.mark.parametrize("discovery_only", [False, True])
def test_offline_cli_publishes_matching_catalog_plan_and_workflow_outputs(
    tmp_path,
    monkeypatch,
    discovery_only,
):
    import hashlib
    import sys

    catalog_path = tmp_path / "input.json"
    catalog_path.write_text(json.dumps(catalog()))
    rules_path = tmp_path / "rules.json"
    # Reverse order ensures offline closure requires more than one pass.
    rules_path.write_text(
        json.dumps(
            [
                {"source": "a", "dependency": "b", "reason": "new API"},
                {"source": "c", "dependency": "a", "reason": "new API"},
            ]
        )
    )
    output = tmp_path / "output"
    github_output = tmp_path / "github-output"
    github_summary = tmp_path / "github-summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    argv = [
        "nightly-plan",
        "--catalog",
        str(catalog_path),
        "--output",
        str(output),
        "--candidate-dependencies",
        str(rules_path),
        "--sources",
        "c",
        "--no-resolve",
    ]
    if discovery_only:
        argv.append("--discovery-only")
    monkeypatch.setattr(sys, "argv", argv)

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("offline planning attempted source resolution")

    monkeypatch.setattr(module, "freeze", unexpected_resolution)
    module.main()
    catalog_bytes = (output / "catalog.json").read_bytes()
    frozen = json.loads(catalog_bytes)
    plan = json.loads((output / "plan.json").read_text())
    assert plan["catalog_sha256"] == hashlib.sha256(catalog_bytes).hexdigest()
    assert plan["sources"] == ["a", "b", "c"]
    assert plan["requested_sources"] == frozen["requested_sources"] == ["c"]
    assert frozen["ci"] == {"run_id": "123", "run_attempt": "2"}
    assert plan["resolution_failures"] == plan["packaging_source_fallbacks"] == []
    assert plan["waves"] == ([] if discovery_only else [["b"], ["a"], ["c"]])
    assert ("phase" in plan) == discovery_only
    workflow = dict(
        line.split("=", 1) for line in github_output.read_text().splitlines()
    )
    assert json.loads(workflow["sources"]) == {"source": ["a", "b", "c"]}
    assert json.loads(workflow["dependency_levels"]) == {
        "include": []
        if discovery_only
        else [
            {"level": 1, "packages": '["b"]'},
            {"level": 2, "packages": '["a"]'},
            {"level": 3, "packages": '["c"]'},
        ],
    }
    assert github_summary.read_text() == (output / "summary.md").read_text()


@pytest.mark.parametrize('failure', [
    module.subprocess.TimeoutExpired(['git', 'ls-remote'], 90),
    OSError('connection unavailable'),
])
def test_expected_repository_errors_allow_packaging_fallback(monkeypatch, failure):
    """Transport and process failures retain diagnostics and permit published source."""
    def unavailable(*args, **kwargs):
        """Represent an expected failure at the Git process boundary."""
        raise failure

    monkeypatch.setattr(module.subprocess, 'run', unavailable)
    entry = module.freeze_packaging({
        'source': 'demo', 'packaging_repository': 'https://example.invalid/demo',
        'packaging_branch_candidates': ['main'],
    })
    assert entry['packaging_source_role'] == 'published-source'
    assert 'Cannot query packaging repository' in entry['packaging_resolution_error']


@pytest.mark.parametrize('upstream_ref', ['a' * 40, 'main'])
def test_packaging_programming_errors_propagate_without_misclassifying_upstream(monkeypatch, upstream_ref):
    """Unexpected packaging failures must not trigger fallback or erase upstream identity."""
    def broken(*args, **kwargs):
        """Represent an internal error rather than a repository availability failure."""
        raise TypeError('invalid internal resolution state')

    monkeypatch.setattr(module, '_resolve_upstream_branch', lambda *args: 'a' * 40)
    monkeypatch.setattr(module, '_packaging_remote', broken)
    entry = {'source': 'demo', 'upstream_repository': 'https://example.invalid/upstream',
             'upstream_ref': upstream_ref, 'packaging_repository': 'https://example.invalid/demo',
             'packaging_branch_candidates': ['main']}
    with pytest.raises(TypeError, match='invalid internal resolution state'):
        module.freeze(entry)

    assert entry['upstream_sha'] == 'a' * 40
    assert 'upstream_resolution_error' not in entry
    assert 'packaging_source_kind' not in entry


def test_missing_packaging_branch_allows_published_source_fallback(monkeypatch):
    """An available repository without a configured branch is an expected fallback."""
    monkeypatch.setattr(module, '_packaging_remote', lambda *args: {'other': 'a' * 40})
    entry = module.freeze_packaging({
        'source': 'demo', 'packaging_repository': 'https://example.invalid/demo',
        'packaging_branch_candidates': ['main'],
    })
    assert entry['packaging_source_role'] == 'published-source'
    assert 'None of the packaging branches exist' in entry['packaging_resolution_error']


def test_discovery_rejects_unsatisfied_archive_requirement_before_preparation():
    from packagetest.upstream_dependencies import parse_requirement_file

    data = {'packages': [{'source': 'consumer', 'build_dependencies': []}],
            'archive_python_packages': [
                {'distribution': 'cmd2', 'binary': 'python3-cmd2',
                 'source': 'python-cmd2', 'version': '3.2.0+ds-1ubuntu1'}]}

    def inspect(entry):
        return (parse_requirement_file('cmd2>=4.0.0\n', filename='requirements.txt', kind='runtime'),
                ['requirements.txt'])

    enriched, selected, _, errors = module.resolve_upstream_dependency_closure(
        data, ['consumer'], freeze_entry=lambda entry: dict(entry, upstream_sha='a' * 40),
        inspect_entry=inspect)
    assert selected == ['consumer']
    assert len(errors) == 1
    assert 'cmd2>=4.0.0' in errors[0]['error']
    assert 'python3-cmd2 3.2.0+ds-1ubuntu1' in errors[0]['error']
    assert enriched['packages'][0]['planned_archive_dependency_additions'] == []
    with pytest.raises(ValueError, match='Unsatisfied archive dependencies'):
        module.plan_catalog(enriched)


def test_scheduling_archive_gate_preserves_candidate_provider_path():
    data = {'packages': [
        {'source': 'consumer', 'build_dependencies': ['provider'],
         'upstream_dependency_requirements': [{'source': 'provider', 'archive_satisfies': False}]},
        {'source': 'provider', 'build_dependencies': []},
    ]}
    _, plan = module.plan_catalog(data)
    assert plan['waves'] == [['provider'], ['consumer']]


def test_tox_test_extras_become_versioned_archive_additions():
    from packagetest.upstream_dependencies import inspect_revision

    data = {'packages': [{'source': 'consumer', 'build_dependencies': [], 'build_depends': 'debhelper-compat (= 13)'}],
            'archive_python_packages': [
                {'distribution': 'cloud-client', 'binary': 'python3-cloud-client',
                 'source': 'cloud-client', 'version': '30.1.0-3'},
                {'distribution': 'locking', 'binary': 'python3-locking',
                 'source': 'locking', 'version': '0.4.1-2'}]}
    files = {'tox.ini': '[testenv]\nextras = cloud\n', 'pyproject.toml': '''
[project.optional-dependencies]
cloud = ["cloud-client>=2.8.1", "locking>=0.4.1"]
docs = ["sphinx>=7"]
'''}

    def freeze(entry):
        return dict(entry, upstream_repository='https://github.com/example/sample', upstream_sha='a' * 40)

    def inspect(entry):
        return inspect_revision(entry, fetch=lambda url: files.get(url.rsplit('/', 1)[-1]))

    enriched, _, _, errors = module.resolve_upstream_dependency_closure(
        data, ['consumer'], freeze_entry=freeze, inspect_entry=inspect)
    assert errors == []
    additions = enriched['packages'][0]['planned_archive_dependency_additions']
    assert {r['binary']: r['requirement'] for r in additions} == {
        'python3-cloud-client': 'cloud-client>=2.8.1', 'python3-locking': 'locking>=0.4.1'}
    assert all(r['kind'] == 'test' for r in additions)


def test_archive_gate_only_blocks_selected_proven_conflicts():
    data = catalog()
    data['packages'][0]['upstream_dependency_requirements'] = [
        {'archive_binary': 'python3-external', 'archive_satisfies': False,
         'file': 'requirements.txt', 'line': 1, 'requirement': 'external>=4', 'archive_version': '3.0-1'}]
    _, plan = module.plan_catalog(data, ['d'])
    assert plan['sources'] == ['d']
    data['packages'][0]['upstream_dependency_requirements'][0]['archive_satisfies'] = None
    _, plan = module.plan_catalog(data)
    assert plan['sources'] == ['a', 'b', 'c', 'd']
