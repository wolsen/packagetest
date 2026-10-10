import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from packagetest.nightly_source import extract_snapshot, prepare_source, snapshot_debian_version


def test_preserve_debian_epoch():
    assert snapshot_debian_version('2:32.0.0-0ubuntu1', '33.0.0~rc1+git20260920.0.abcdef0') == '2:33.0.0~rc1+git20260920.0.abcdef0-0ubuntu1~hibiscus1'
    assert snapshot_debian_version('1.0-1', '1.1+git20260920.2.abcdef0') == '1.1+git20260920.2.abcdef0-0ubuntu1~hibiscus1'


def test_missing_pin_fails_with_persistent_evidence(tmp_path):
    destination = tmp_path / 'result'
    with pytest.raises(ValueError, match='plan-pinned'):
        prepare_source({'source': 'test'}, destination)
    report = json.loads((destination / 'resolution.json').read_text())
    assert report['status'] == 'FAILED'
    assert 'plan-pinned' in report['error']
    with pytest.raises(FileExistsError):
        prepare_source({'source': 'test'}, destination)


def test_resolution_error_not_retried_as_master(tmp_path):
    with pytest.raises(ValueError, match='missing stable branch'):
        prepare_source({'source': 'test', 'upstream_resolution_error': 'missing stable branch'}, tmp_path / 'result')
    with pytest.raises(ValueError, match='dependency metadata unavailable'):
        prepare_source({'source': 'test', 'upstream_dependency_resolution_error':
                        'dependency metadata unavailable'}, tmp_path / 'dependency-result')


def importer_packaging_repository(tmp_path, *, version='1.0-1', binary='python3-demo'):
    repository = tmp_path / 'repository'
    (repository / 'debian').mkdir(parents=True)
    (repository / 'debian/control').write_text(
        'Source: demo\nMaintainer: Test <test@example.invalid>\n\n'
        f'Package: {binary}\nArchitecture: all\nDescription: test\n')
    (repository / 'debian/changelog').write_text(
        f'demo ({version}) unstable; urgency=medium\n\n  * Test.\n\n'
        ' -- Test <test@example.invalid>  Thu, 01 Jan 2026 00:00:00 +0000\n')
    subprocess.run(['git', 'init', '-q', '-b', 'ubuntu/resolute'], cwd=repository, check=True)
    subprocess.run(['git', 'add', '.'], cwd=repository, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid',
                    'commit', '-qm', 'packaging'], cwd=repository, check=True)
    revision = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    return repository, revision


def test_ubuntu_importer_checkout_matches_archive_identity(tmp_path):
    from packagetest.nightly_source import Preparation, checkout_packaging_tree
    repository, revision = importer_packaging_repository(tmp_path)
    build = Preparation(tmp_path / 'result')
    checkout, metadata = checkout_packaging_tree(build, {
        'source': 'demo', 'archive_version': '1.0-1', 'binaries': ['python3-demo'],
        'packaging_source_kind': 'git', 'packaging_source_role': 'ubuntu-importer',
        'packaging_source_repository': str(repository),
        'packaging_branch': 'ubuntu/resolute', 'packaging_sha': revision,
    })
    assert checkout.is_dir()
    assert metadata['role'] == 'ubuntu-importer'


def test_ubuntu_importer_checkout_rejects_archive_version_mismatch(tmp_path):
    from packagetest.nightly_source import Preparation, checkout_packaging_tree
    repository, revision = importer_packaging_repository(tmp_path, version='0.9-1')
    build = Preparation(tmp_path / 'result')
    with pytest.raises(ValueError, match='differs from archive'):
        checkout_packaging_tree(build, {
            'source': 'demo', 'archive_version': '1.0-1', 'binaries': ['python3-demo'],
            'packaging_source_kind': 'git', 'packaging_source_role': 'ubuntu-importer',
            'packaging_source_repository': str(repository),
            'packaging_branch': 'ubuntu/resolute', 'packaging_sha': revision,
        })


def test_archive_extraction_rejects_path_traversal(tmp_path):
    archive = tmp_path / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as out:
        member = tarfile.TarInfo('root/../../escaped')
        member.size = 1
        out.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='Unsafe'):
        extract_snapshot(archive, tmp_path / 'output')
    assert not (tmp_path / 'escaped').exists()


def test_archive_extraction_strips_one_root(tmp_path):
    archive = tmp_path / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as out:
        member = tarfile.TarInfo('root/README')
        member.size = 3
        out.addfile(member, io.BytesIO(b'abc'))
    extract_snapshot(archive, tmp_path / 'output')
    assert (tmp_path / 'output' / 'README').read_text() == 'abc'


def test_debian_maintainer_is_preserved_when_deriving_ubuntu_package(tmp_path):
    from packagetest.nightly_source import ubuntu_maintainer
    path = tmp_path / 'control'
    path.write_text('Source: sample\nMaintainer: Debian Team <team@debian.org>\n\nPackage: sample\nDescription: test\n')
    ubuntu_maintainer(path)
    result = path.read_text()
    assert 'XSBC-Original-Maintainer: Debian Team <team@debian.org>' in result
    assert 'Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>' in result
    ubuntu_maintainer(path)
    assert path.read_text() == result


def test_existing_ubuntu_maintainer_is_byte_stable(tmp_path):
    from packagetest.nightly_source import ubuntu_maintainer
    path = tmp_path / 'control'
    content = ('Source: sample\n'
               'Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>\n\n'
               'Package: sample\nDescription: test\n')
    path.write_text(content)
    ubuntu_maintainer(path)
    assert path.read_text() == content


def test_packaging_tree_is_cloned_at_plan_pinned_launchpad_commit(tmp_path):
    from packagetest.nightly_source import Preparation, checkout_packaging_tree

    repository = tmp_path / 'packaging-repository'
    (repository / 'debian').mkdir(parents=True)
    (repository / 'debian/control').write_text(
        'Source: demo\nMaintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>\n\n'
        'Package: demo\nArchitecture: all\nDescription: demo\n')
    (repository / 'debian/changelog').write_text(
        'demo (1.0-1) unstable; urgency=medium\n\n  * Test.\n\n'
        ' -- Test <test@example.invalid>  Thu, 01 Jan 2026 00:00:00 +0000\n')
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'packaging'], check=True)
    revision = subprocess.check_output(
        ['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
    build = Preparation(tmp_path / 'result')

    checkout, metadata = checkout_packaging_tree(build, {
        'source': 'demo', 'packaging_source_kind': 'git',
        'packaging_source_role': 'ubuntu-openstack',
        'packaging_source_repository': str(repository), 'packaging_branch': 'master',
        'packaging_sha': revision, 'packaging_upstream_branch': 'upstream-hibiscus',
        'packaging_upstream_sha': '2' * 40, 'packaging_pristine_tar_sha': '3' * 40,
    })

    assert (checkout / 'debian/control').is_file()
    assert metadata['kind'] == 'git'
    assert metadata['sha'] == revision
    assert subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                   text=True).strip() == revision


def test_upstream_test_dependencies_only_change_build_dependencies(tmp_path):
    from packagetest.nightly_source import upstream_dependency_adjustments
    tree = tmp_path / 'watcher'
    (tree / 'debian').mkdir(parents=True)
    control = tree / 'debian/control'
    control.write_text('''Source: watcher
Build-Depends: debhelper-compat (= 13),
Build-Depends-Indep:
 python3-stestr,

Package: python3-watcher
Architecture: all
Depends:
 python3-runtime,
 ${python3:Depends},
Description: demo
''')
    entry = {'upstream_dependency_requirements': [
        {'distribution': 'wsgi-intercept', 'requirement': 'wsgi-intercept>=1.7',
         'kind': 'test', 'file': 'test-requirements.txt', 'line': 3,
         'archive_binary': 'python3-wsgi-intercept',
         'archive_source': 'python-wsgi-intercept', 'archive_version': '1.13.1-1',
         'archive_satisfies': True},
        {'distribution': 'gabbi', 'requirement': 'gabbi>=1.35',
         'kind': 'test', 'file': 'test-requirements.txt', 'line': 4,
         'archive_binary': 'python3-gabbi', 'archive_source': 'python-gabbi',
         'archive_version': '3.0.0-1', 'archive_satisfies': True},
    ]}

    actions = upstream_dependency_adjustments(entry, tree)
    source, binary = control.read_text().split('\n\n', 1)
    assert 'python3-gabbi' in source and 'python3-wsgi-intercept' in source
    assert 'python3-gabbi' not in binary and 'python3-wsgi-intercept' not in binary
    assert {item['package'] for item in actions} == {
        'python3-gabbi', 'python3-wsgi-intercept'}
    assert all(item['scopes'] == ['build'] for item in actions)


def test_upstream_runtime_dependency_is_recorded_but_not_automatically_added(tmp_path):
    from packagetest.nightly_source import upstream_dependency_adjustments
    tree = tmp_path / 'service'
    (tree / 'debian').mkdir(parents=True)
    control = tree / 'debian/control'
    control.write_text('''Source: service
Build-Depends: debhelper-compat (= 13),

Package: python3-service
Architecture: all
Depends:
 ${python3:Depends},
Description: demo
''')
    entry = {'upstream_dependency_requirements': [{
        'distribution': 'werkzeug', 'requirement': 'Werkzeug>=3', 'kind': 'runtime',
        'file': 'requirements.txt', 'line': 2, 'archive_binary': 'python3-werkzeug',
        'archive_source': 'python-werkzeug', 'archive_version': '3.1.5-1',
        'archive_satisfies': True,
    }]}

    actions = upstream_dependency_adjustments(entry, tree)
    assert control.read_text() == '''Source: service
Build-Depends: debhelper-compat (= 13),

Package: python3-service
Architecture: all
Depends:
 ${python3:Depends},
Description: demo
'''
    assert actions == []


def test_new_console_script_uses_unambiguous_existing_install_owner(tmp_path):
    from packagetest.nightly_source import source_evolution
    repository = tmp_path / 'upstream'
    repository.mkdir()
    (repository / 'setup.cfg').write_text('''[metadata]
name = demo
[entry_points]
console_scripts =
 old-command = demo.cmd:old
openstack.demo.plugin =
 old = demo.plugin:Old
''')
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'release'], check=True)
    base = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'],
                                   text=True).strip()
    subprocess.run(['git', '-C', str(repository), 'tag', '1.0.0'], check=True)
    tree = tmp_path / 'candidate'
    (tree / 'debian').mkdir(parents=True)
    (tree / 'setup.cfg').write_text('''[metadata]
name = demo
[entry_points]
console_scripts =
 old-command = demo.cmd:old
 new-command = demo.cmd:new
openstack.demo.plugin =
 old = demo.plugin:Old
 new = demo.plugin:New
''')
    manifest = tree / 'debian/demo-api.install'
    manifest.write_text('usr/bin/old-command\nusr/share/demo/*\n')
    selected = {'sha': base, 'base_tag': '1.0.0', 'base_tag_sha': base, 'commits_since_tag': 2,
                'commits': [{'sha': 'a' * 40, 'timestamp': 1, 'author': 'Dev',
                             'subject': 'Add command'}]}

    report, actions = source_evolution(
        {'source': 'demo', 'archive_version': '1.0.0-1'}, repository, selected, tree)

    assert 'usr/bin/new-command' in manifest.read_text().splitlines()
    assert actions[0]['manifest'] == 'demo-api.install'
    findings = {item['name']: item for item in report['introduced_entry_points']}
    assert findings['new-command']['packaging'] == 'assigned-to-existing-binary'
    assert findings['new']['packaging'] == 'included-with-python-metadata'
    assert report['human_binary_package_review_required'] is True
    assert report['commit_delta']['comparison_tag_matches_archive_version'] is True
    assert report['commit_delta']['count'] == 0


def test_new_console_script_with_ambiguous_owner_requires_human_decision(tmp_path):
    from packagetest.nightly_source import source_evolution
    repository = tmp_path / 'upstream'
    repository.mkdir()
    (repository / 'setup.cfg').write_text('''[entry_points]
console_scripts =
 one = demo:one
 two = demo:two
''')
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'release'], check=True)
    base = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'],
                                   text=True).strip()
    subprocess.run(['git', '-C', str(repository), 'tag', '1.0.0'], check=True)
    tree = tmp_path / 'candidate'
    (tree / 'debian').mkdir(parents=True)
    (tree / 'setup.cfg').write_text('''[entry_points]
console_scripts =
 one = demo:one
 two = demo:two
 three = demo:three
''')
    (tree / 'debian/one.install').write_text('usr/bin/one\n')
    (tree / 'debian/two.install').write_text('usr/bin/two\n')

    report, actions = source_evolution(
        {'source': 'demo', 'archive_version': '0.9-1'}, repository,
        {'sha': base, 'base_tag': '1.0.0', 'base_tag_sha': base, 'commits_since_tag': 0,
         'commits': []}, tree)

    assert actions == []
    finding = report['introduced_entry_points'][0]
    assert finding['name'] == 'three'
    assert finding['packaging'] == 'human-decision-required'
    assert finding['candidate_manifests'] == ['one.install', 'two.install']
    assert report['commit_delta']['basis'] == 'nearest-upstream-release-tag'


def test_official_package_tag_delta_lists_every_snapshot_commit(tmp_path):
    from packagetest.nightly_source import _official_commit_delta
    repository = tmp_path / 'upstream'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty',
                    '-qm', 'official release'], check=True)
    base = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'],
                                   text=True).strip()
    subprocess.run(['git', '-C', str(repository), 'tag', '2.3.0'], check=True)
    for subject in ('Add API command', 'Move service to WSGI'):
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty',
                        '-qm', subject], check=True)
    tip = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'],
                                  text=True).strip()

    delta = _official_commit_delta(
        {'archive_version': '1:2.3.0-0ubuntu1'}, repository,
        {'sha': tip, 'base_tag': '2.4.0', 'base_tag_sha': base,
         'commits_since_tag': 0, 'commits': []})

    assert delta['basis'] == 'official-package-upstream-tag'
    assert delta['comparison_tag'] == '2.3.0'
    assert delta['count'] == 2
    assert [item['subject'] for item in delta['commits']] == [
        'Add API command', 'Move service to WSGI']


def test_prepared_control_metadata_captures_binary_and_autopkgtest_relationships(tmp_path):
    import importlib.util
    path = Path(__file__).parents[1] / 'scripts/prepare-nightly-source.py'
    spec = importlib.util.spec_from_file_location('prepare_nightly_source', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tree = tmp_path / 'source'
    (tree / 'debian/tests').mkdir(parents=True)
    (tree / 'debian/control').write_text('''Source: demo
Build-Depends: debhelper-compat (= 13), python3-setuptools

Package: python3-demo
Architecture: all
Depends: ${python3:Depends}, python3-runtime
Description: demo

Package: demo-api
Architecture: all
Depends: python3-demo (= ${binary:Version})
Description: api
''')
    (tree / 'debian/tests/control').write_text('''Tests: smoke
Depends: @, python3-testtools
Restrictions: superficial
''')

    value = module.control_metadata(tree)

    assert value['build_depends']['Build-Depends'].startswith('debhelper-compat')
    assert [item['package'] for item in value['binary_packages']] == [
        'python3-demo', 'demo-api']
    assert value['autopkgtests'][0]['depends'] == '@, python3-testtools'


def test_only_complete_already_applied_patch_is_omitted(tmp_path):
    from packagetest.nightly_source import already_applied_patches
    patches = tmp_path / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('fix.patch\n')
    (patches / 'fix.patch').write_text('--- a/code\n+++ b/code\n@@ -1,2 +1,2 @@\n context\n-vulnerable\n+fixed\n')
    code = tmp_path / 'code'
    code.write_text('context\nvulnerable\n')
    assert already_applied_patches(tmp_path) == []
    code.write_text('context\nfixed\n')
    assert already_applied_patches(tmp_path)[0]['name'] == 'fix.patch'
    assert code.read_text() == 'context\nfixed\n'
    assert (patches / 'series').read_text().startswith('# Fully present upstream')


def test_semantically_equivalent_post_release_commit_supersedes_patch(tmp_path):
    from packagetest.nightly_source import superseded_upstream_patches
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'master'], cwd=checkout, check=True)
    target = checkout / 'sample/test_store.py'
    target.parent.mkdir()
    target.write_text('''class TestImage:\n    def test_new_image_with_location(self):\n        create_image()\n''')
    subprocess.run(['git', 'add', '.'], cwd=checkout, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid',
                    'commit', '-qm', 'release'], cwd=checkout, check=True)
    target.write_text('''class TestImage:\n    @mock.patch("glance.common.utils.socket.getaddrinfo")\n    def test_new_image_with_location(self, mock_getaddrinfo):\n        # Avoid DNS resolution in offline builds.\n        mock_getaddrinfo.return_value = [(None, None, None, None, ("192.0.2.1", 80))]\n        create_image()\n''')
    subprocess.run(['git', 'add', '.'], cwd=checkout, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid',
                    'commit', '-qm', 'Do not rely on DNS resolution'], cwd=checkout, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                                     text=True).strip()
    tree = tmp_path / 'tree'
    shutil.copytree(checkout / 'sample', tree / 'sample')
    patches = tree / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('offline.patch\n')
    (patches / 'offline.patch').write_text('''Subject: Mock DNS in the offline image location test
--- a/sample/test_store.py
+++ b/sample/test_store.py
@@ -1,3 +1,7 @@
 class TestImage:
-    def test_new_image_with_location(self):
+    @mock.patch("glance.common.utils.socket.getaddrinfo")
+    def test_new_image_with_location(self, mock_getaddrinfo):
+        mock_getaddrinfo.return_value = [
+            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 80))]
         create_image()
''')

    result = superseded_upstream_patches(
        tree, checkout, {'commits': [{'sha': commit, 'subject': 'Do not rely on DNS resolution'}]})

    assert result[0]['action'] == 'omit-upstream-superseded-patch'
    assert result[0]['upstream_commit']['sha'] == commit
    assert result[0]['current_overlap'] >= 0.7
    assert (patches / 'series').read_text().startswith('# Superseded by verified upstream commit')


def test_unrelated_upstream_change_does_not_supersede_failed_patch(tmp_path):
    from packagetest.nightly_source import superseded_upstream_patches
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'master'], cwd=checkout, check=True)
    target = checkout / 'sample/service.py'
    target.parent.mkdir()
    target.write_text('def launch_service():\n    return legacy_backend()\n')
    subprocess.run(['git', 'add', '.'], cwd=checkout, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid',
                    'commit', '-qm', 'unrelated documentation change'], cwd=checkout, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                                     text=True).strip()
    tree = tmp_path / 'tree'
    shutil.copytree(checkout / 'sample', tree / 'sample')
    patches = tree / 'debian/patches'
    patches.mkdir(parents=True)
    (patches / 'series').write_text('feature.patch\n')
    (patches / 'feature.patch').write_text('''--- a/sample/service.py
+++ b/sample/service.py
@@ -1,2 +1,3 @@
+@retry_on_transport_failure
 def launch_service():
-    return legacy_backend()
+    return resilient_cluster_backend()
''')

    result = superseded_upstream_patches(
        tree, checkout, {'commits': [{'sha': commit, 'subject': 'unrelated'}]})

    assert result == []
    assert (patches / 'series').read_text() == 'feature.patch\n'


def test_packaging_proposal_is_git_patch_with_unselected_human_review_target(tmp_path):
    from packagetest.nightly_source import write_packaging_proposal
    baseline = tmp_path / 'baseline'
    tree = tmp_path / 'tree'
    (baseline / 'tests').mkdir(parents=True)
    (tree / 'debian/tests').mkdir(parents=True)
    (baseline / 'control').write_text('Source: demo\n\nPackage: demo\nDepends: old\n')
    (tree / 'debian/control').write_text('Source: demo\n\nPackage: demo\nDepends: old, new\n')
    script = tree / 'debian/tests/smoke'
    script.write_text('#!/bin/sh\nexit 0\n')
    script.chmod(0o755)
    output = tmp_path / 'proposal'
    entry = {
        'source': 'demo',
        'archive_version': '1.0-1',
        'archive_source': {'url': 'https://archive.example/demo.dsc', 'sha256': 'a' * 64},
        'packaging_repository': 'https://git.example/ubuntu/demo',
        'ubuntu_importer_repository': 'https://git.example/ubuntu-importer/demo',
        'archive_packaging_repository': 'https://git.example/debian/demo',
        'packaging_branch_candidates': ['stable/2026.2', 'master'],
    }
    proposal = write_packaging_proposal(
        entry, baseline, tree, output,
        [{'action': 'replace-packaging-file', 'name': 'control', 'reason': 'new runtime dependency'}])

    assert proposal['status'] == 'candidate'
    assert proposal['human_review_required'] is True
    assert proposal['selected_target'] is None
    assert {item['role'] for item in proposal['destination_candidates']} == {
        'ubuntu-openstack', 'ubuntu-importer', 'archive'}
    patch = (output / 'packaging-proposal.patch').read_text()
    assert 'diff --git a/debian/control b/debian/control' in patch
    assert 'new file mode 100755' in patch
    checkout = tmp_path / 'checkout'
    (checkout / 'debian/tests').mkdir(parents=True)
    (checkout / 'debian/control').write_text((baseline / 'control').read_text())
    subprocess.run(['git', 'apply', '--check', str(output / 'packaging-proposal.patch')],
                   cwd=checkout, check=True)


def test_packaging_proposal_is_absent_without_temporary_changes(tmp_path):
    from packagetest.nightly_source import write_packaging_proposal
    entry = {'source': 'demo'}
    assert write_packaging_proposal(entry, tmp_path, tmp_path, tmp_path / 'proposal', []) is None
    assert not (tmp_path / 'proposal').exists()


def component_fixture(tmp_path):
    import hashlib
    archive = tmp_path / 'sample_1.0.orig-assets.tar.gz'
    with tarfile.open(archive, 'w:gz') as out:
        member = tarfile.TarInfo('assets/data.js')
        member.size = 3
        out.addfile(member, io.BytesIO(b'abc'))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    dsc = tmp_path / 'sample_1.0-1.dsc'
    dsc.write_text(f'Checksums-Sha256:\n {digest} {archive.stat().st_size} {archive.name}\n')
    tree = tmp_path / 'snapshot'
    tree.mkdir()
    return dsc, archive, tree, digest


def test_snapshot_preserves_verified_component_bytes_layout_and_provenance(tmp_path):
    from packagetest.nightly_source import preserve_orig_components
    dsc, archive, tree, digest = component_fixture(tmp_path)
    result = preserve_orig_components(dsc, 'sample', '2.0+git123', tree, tmp_path)
    copied = tmp_path / 'sample_2.0+git123.orig-assets.tar.gz'
    assert copied.read_bytes() == archive.read_bytes()
    assert (tree / 'assets/data.js').read_bytes() == b'abc'
    assert result == [{'component': 'assets', 'archive_file': archive.name,
                       'snapshot_file': copied.name, 'sha256': digest}]


def test_snapshot_component_rejects_tampered_archive_before_extraction(tmp_path):
    from packagetest.nightly_source import preserve_orig_components
    dsc, archive, tree, _ = component_fixture(tmp_path)
    archive.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='checksum mismatch'):
        preserve_orig_components(dsc, 'sample', '2.0', tree, tmp_path)
    assert not (tree / 'assets').exists()


def test_snapshot_component_cannot_replace_upstream_directory(tmp_path):
    from packagetest.nightly_source import preserve_orig_components
    dsc, _, tree, _ = component_fixture(tmp_path)
    (tree / 'assets').mkdir()
    with pytest.raises(ValueError, match='collides'):
        preserve_orig_components(dsc, 'sample', '2.0', tree, tmp_path)
