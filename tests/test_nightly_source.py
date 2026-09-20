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
