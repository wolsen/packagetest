import io
import json
import tarfile
from pathlib import Path

import pytest
from packagetest.artifacts import sha256
from packagetest.locked import load_lock
from packagetest.snapshot import canonical_sdist, snapshot_version
from packagetest.versioning import debian_compare


def test_snapshot_version_uses_commit_date_and_sorts_after_release():
    version = snapshot_version('6.9.0', 1789374948, 3, '7045af07a0654247ad9f3dfd416622afed3e1489')
    assert version == '6.9.0+git20260914.3.7045af0'
    assert debian_compare('6.9.0', version) == -1
    assert debian_compare(version, '6.9.1') == -1
    assert snapshot_version('7.0.0.0rc1', 1789374948, 3, 'abcdef0123').startswith('7.0.0~rc1+git')
    assert snapshot_version('6.9.0', 1789374948, 0, 'abcdef0123') == '6.9.0+git20260914.0.abcdef0'
    with pytest.raises(ValueError, match='negative'):
        snapshot_version('6.9.0', 1789374948, -1, 'abcdef0123')


def sdist(path, timestamp, *, version='6.9.0+git20260914.3.7045af0', missing=None):
    with tarfile.open(path, 'w:gz') as archive:
        for name, content in [('PKG-INFO', f'Name: oslo.i18n\nVersion: {version}\n'), ('AUTHORS', 'A\n'), ('ChangeLog', 'C\n')]:
            if name == missing:
                continue
            member = tarfile.TarInfo('oslo.i18n-snapshot/' + name)
            data = content.encode()
            member.size = len(data)
            member.mtime = timestamp
            member.uid = timestamp
            archive.addfile(member, io.BytesIO(data))


def test_sdist_normalization_preserves_metadata_and_repeats(tmp_path):
    results = []
    for epoch in (10, 20):
        source, output = tmp_path / f'{epoch}.tgz', tmp_path / f'{epoch}.orig.tar.gz'
        sdist(source, epoch)
        canonical_sdist(source, output, epoch=1789374948, version='6.9.0+git20260914.3.7045af0')
        results.append(sha256(output))
    assert results[0] == results[1]


@pytest.mark.parametrize('kwargs', [{'missing': 'ChangeLog'}, {'version': '6.9.0'}])
def test_snapshot_rejects_missing_or_wrong_generated_metadata(tmp_path, kwargs):
    source = tmp_path / 'source.tgz'
    sdist(source, 10, **kwargs)
    with pytest.raises(ValueError):
        canonical_sdist(source, tmp_path / 'out.tgz', epoch=10, version='6.9.0+git20260914.3.7045af0')


def test_snapshot_requires_git_and_tool_pins(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'config/locks/oslo-i18n-stonking-snapshot.json'
    lock = load_lock(source)
    lock['packages'][0]['input']['snapshot']['sha'] = 'master'
    invalid = tmp_path / 'invalid.json'
    invalid.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match='full Git'):
        load_lock(invalid)


def test_portable_sdist_removes_umask_variation_and_preserves_executable(tmp_path):
    outputs = []
    for mode in (0o644, 0o664):
        source = tmp_path / f'{mode}.tgz'
        with tarfile.open(source, 'w:gz') as archive:
            for name, data in [('AUTHORS', b'A'), ('ChangeLog', b'C'), ('PKG-INFO', b'Version: 1.0\n'), ('run', b'#!/bin/sh')]:
                member = tarfile.TarInfo('project/' + name)
                member.size = len(data)
                member.mode = mode | (0o111 if name == 'run' else 0)
                archive.addfile(member, io.BytesIO(data))
        output = tmp_path / f'{mode}-normalized.tgz'
        canonical_sdist(source, output, epoch=100, version='1.0')
        outputs.append(output.read_bytes())
        with tarfile.open(output) as archive:
            assert archive.getmember('project/run').mode == 0o755
            assert archive.getmember('project/AUTHORS').mode == 0o644
    assert outputs[0] == outputs[1]


def test_snapshot_rc_python_version_is_canonical():
    from packagetest.snapshot import snapshot_pep440_version
    assert snapshot_pep440_version('33.0.0~rc1+git20260911.2.8cd693a') == '33.0.0rc1+git20260911.2.8cd693a'
    assert snapshot_pep440_version('33.0.0~b2+git20260811.0.8cd693a') == '33.0.0b2+git20260811.0.8cd693a'
    assert snapshot_pep440_version('6.9.0+git20260903.2.8fe7cb0') == '6.9.0+git20260903.2.8fe7cb0'
