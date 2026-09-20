import io
import json
from pathlib import Path
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


def test_patch_adaptation_requires_exact_source_and_patch_checksums(tmp_path):
    import hashlib
    from packagetest.nightly_source import packaging_adjustments
    tree = tmp_path / 'tree'
    (tree / 'debian' / 'patches').mkdir(parents=True)
    (tree / 'debian' / 'patches' / 'series').write_text('fix.patch\n')
    (tree / 'debian' / 'patches' / 'fix.patch').write_text('old patch')
    (tree / 'test.py').write_text('upstream fix')
    config = tmp_path / 'config' / 'sample'
    config.mkdir(parents=True)
    spec = {'archive_dsc_sha256': 'a' * 64, 'drop_patches': [{'name': 'fix.patch', 'sha256': hashlib.sha256(b'old patch').hexdigest(), 'upstream_file': 'test.py', 'upstream_file_sha256': hashlib.sha256(b'upstream fix').hexdigest(), 'reason': 'upstream has fix'}]}
    (config / 'adjustments.json').write_text(json.dumps(spec))
    entry = {'source': 'sample', 'archive_source': {'sha256': 'b' * 64}}
    with pytest.raises(ValueError, match='changed archive'):
        packaging_adjustments(entry, tree, config.parent)
    entry['archive_source']['sha256'] = 'a' * 64
    (tree / 'test.py').write_text('changed upstream')
    with pytest.raises(ValueError, match='checksum guard'):
        packaging_adjustments(entry, tree, config.parent)
    assert (tree / 'debian' / 'patches' / 'series').read_text() == 'fix.patch\n'
    (tree / 'test.py').write_text('upstream fix')
    actions = packaging_adjustments(entry, tree, config.parent)
    assert actions[0]['action'] == 'omit-obsolete-patch'
    assert (tree / 'debian' / 'patches' / 'series').read_text() == '# Superseded upstream: fix.patch\n'


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


def test_replacement_packaging_requires_both_checksums(tmp_path):
    from packagetest.nightly_source import packaging_adjustments
    from packagetest.artifacts import sha256
    tree = tmp_path / 'tree'
    (tree / 'debian').mkdir(parents=True)
    original = tree / 'debian/rules'
    original.write_text('old rules\n')
    cfg = tmp_path / 'config/sample'
    cfg.mkdir(parents=True)
    replacement = cfg / 'rules'
    replacement.write_text('new rules\n')
    spec = {'archive_dsc_sha256': 'a' * 64, 'replace_files': [
        {'name': 'rules', 'sha256': sha256(original), 'replacement': 'rules',
         'replacement_sha256': sha256(replacement)}]}
    (cfg / 'adjustments.json').write_text(json.dumps(spec))
    entry = {'source': 'sample', 'archive_source': {'sha256': 'a' * 64}}
    assert packaging_adjustments(entry, tree, cfg.parent)[0]['action'] == 'replace-packaging-file'
    assert original.read_text() == 'new rules\n'
    with pytest.raises(ValueError, match='checksum guard'):
        packaging_adjustments(entry, tree, cfg.parent)


def test_add_packaging_tests_are_guarded_and_keep_explicit_mode(tmp_path):
    import hashlib
    import json
    from packagetest.nightly_source import packaging_adjustments
    tree = tmp_path / 'source'
    (tree / 'debian').mkdir(parents=True)
    config = tmp_path / 'config' / 'demo'
    config.mkdir(parents=True)
    content = b'#!/bin/sh\necho tested\n'
    (config / 'script').write_bytes(content)
    entry = {'source': 'demo', 'archive_source': {'sha256': 'a' * 64}}
    item = {'name': 'tests/run', 'replacement': 'script', 'mode': '0755',
            'replacement_sha256': hashlib.sha256(content).hexdigest()}
    spec = {'archive_dsc_sha256': 'a' * 64, 'add_files': [item]}
    (config / 'adjustments.json').write_text(json.dumps(spec))
    assert packaging_adjustments(entry, tree, config.parent)[0]['action'] == 'add-packaging-file'
    assert (tree / 'debian/tests/run').read_bytes() == content
    assert (tree / 'debian/tests/run').stat().st_mode & 0o777 == 0o755
    with pytest.raises(ValueError, match='already exists'):
        packaging_adjustments(entry, tree, config.parent)


@pytest.mark.parametrize('violation', ['traversal', 'digest', 'mode', 'destination-symlink', 'replacement-symlink'])
def test_add_packaging_tests_reject_unsafe_destinations_and_content(tmp_path, violation):
    import hashlib
    import json
    from packagetest.nightly_source import packaging_adjustments
    tree = tmp_path / 'source'
    (tree / 'debian').mkdir(parents=True)
    config = tmp_path / 'config' / 'demo'
    config.mkdir(parents=True)
    content = b'candidate test'
    (config / 'script').write_bytes(content)
    item = {'name': 'tests/run', 'replacement': 'script', 'mode': '0755',
            'replacement_sha256': hashlib.sha256(content).hexdigest()}
    if violation == 'traversal':
        item['name'] = '../outside'
    elif violation == 'digest':
        item['replacement_sha256'] = '0' * 64
    elif violation == 'mode':
        item['mode'] = '4755'
    elif violation == 'destination-symlink':
        (tree / 'debian/tests').symlink_to(tmp_path, target_is_directory=True)
    else:
        (config / 'script').rename(config / 'real')
        (config / 'script').symlink_to(config / 'real')
    (config / 'adjustments.json').write_text(json.dumps({'archive_dsc_sha256': 'a' * 64, 'add_files': [item]}))
    with pytest.raises(ValueError):
        packaging_adjustments({'source': 'demo', 'archive_source': {'sha256': 'a' * 64}}, tree, config.parent)
    assert not (tree / 'debian/tests/run').exists()
