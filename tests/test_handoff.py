import json
import subprocess
from pathlib import Path

import pytest

from packagetest.artifacts import sha256
from packagetest.handoff import build_repository, collect_producers, dependency_waves, lock_digest, stamp_generation


def bundle(tmp_path):
    root = tmp_path / 'download' / 'producer'
    directory = root / 'artifacts' / 'sample' / 'binary'
    directory.mkdir(parents=True)
    package_dir = tmp_path / 'deb'
    (package_dir / 'DEBIAN').mkdir(parents=True)
    (package_dir / 'DEBIAN' / 'control').write_text('Package: sample\nVersion: 1.0-1\nArchitecture: all\nMaintainer: Test <test@example.org>\nDescription: test package\n')
    deb = directory / 'sample_1.0-1_all.deb'
    subprocess.run(['dpkg-deb', '--build', str(package_dir), str(deb)], check=True, capture_output=True)
    info = directory / 'sample.buildinfo'
    info.write_text('Source: sample\nVersion: 1.0-1\nInstalled-Build-Depends: base-files (= 1)\n')
    changes = directory / 'sample.changes'
    checksums = ''.join(f' {sha256(p)} {p.stat().st_size} {p.name}\n' for p in (deb, info))
    changes.write_text('Source: sample\nVersion: 1.0-1\nArchitecture: all\nDistribution: resolute\nChecksums-Sha256:\n' + checksums)
    target = {'suite': 'resolute', 'architecture': 'amd64'}
    lock = {'target': target, 'packages': [{'source': 'sample', 'version': '1.0-1', 'expected_binaries': ['sample']}]}
    binary = {'file': deb.name, 'package': 'sample', 'version': '1.0-1', 'architecture': 'all', 'sha256': sha256(deb)}
    manifest = {'target': target, 'lock_sha256': lock_digest(lock), 'result': 'SUCCEEDED',
                'packages': [{'source': 'sample', 'version': '1.0-1', 'result': 'SUCCEEDED', 'binaries': [binary]}]}
    (root / 'build-lock.json').write_text(json.dumps(lock))
    path = root / 'generation-manifest.json'
    path.write_text(json.dumps(manifest))
    stamp_generation(path, run_id='123', run_attempt='1')
    return root, lock, deb


def collect(root, lock, **kwargs):
    return collect_producers(root.parent, {'sample': lock}, run_id='123', run_attempt='1', target=lock['target'], **kwargs)


def test_handoff_reconstructs_repository_with_relative_paths(tmp_path):
    root, lock, deb = bundle(tmp_path)
    result = collect(root, lock)
    repo = tmp_path / 'repo'
    provenance = build_repository(result, repo)
    assert 'Filename: pool/sample_1.0-1_all.deb' in (repo / 'Packages').read_text()
    assert provenance['packages'][0]['path'] == 'pool/sample_1.0-1_all.deb'
    assert provenance['packages'][0]['ci']['run_id'] == '123'
    assert sha256(repo / provenance['packages'][0]['path']) == sha256(deb)


@pytest.mark.parametrize('change', ['run', 'attempt', 'target', 'failed', 'lock', 'record', 'missing', 'corrupt'])
def test_rejects_wrong_producer(tmp_path, change):
    root, lock, deb = bundle(tmp_path)
    path = root / 'generation-manifest.json'
    manifest = json.loads(path.read_text())
    if change == 'run': manifest['ci']['run_id'] = '122'
    if change == 'attempt': manifest['ci']['run_attempt'] = '2'
    if change == 'target': manifest['target']['suite'] = 'noble'
    if change == 'failed': manifest['result'] = 'FAILED'
    if change == 'lock': manifest['lock_sha256'] = '0' * 64
    if change == 'record': manifest['packages'][0]['binaries'][0]['sha256'] = '0' * 64
    if change == 'missing': manifest['packages'] = []
    if change == 'corrupt': deb.write_bytes(b'corrupt')
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        collect(root, lock)


def test_rejects_changed_bytes_between_collect_and_repo(tmp_path):
    root, lock, deb = bundle(tmp_path)
    producers = collect(root, lock)
    deb.write_bytes(b'changed after download validation')
    with pytest.raises(ValueError, match='changed'):
        build_repository(producers, tmp_path / 'repo')
    assert not (tmp_path / 'repo').exists()


def test_refuses_duplicate_producers(tmp_path):
    import shutil
    root, lock, _ = bundle(tmp_path)
    shutil.copytree(root, root.parent / 'duplicate')
    with pytest.raises(ValueError, match='Duplicate producer'):
        collect(root, lock)


def test_parallel_waves_and_bad_graphs():
    assert dependency_waves([{'source': 'a'}, {'source': 'b'}, {'source': 'c', 'depends_on': ['a']},
                             {'source': 'd', 'depends_on': ['b', 'c']}]) == [['a', 'b'], ['c'], ['d']]
    with pytest.raises(ValueError, match='missing'):
        dependency_waves([{'source': 'a', 'depends_on': ['missing']}])
    with pytest.raises(ValueError, match='cycle'):
        dependency_waves([{'source': 'a', 'depends_on': ['b']}, {'source': 'b', 'depends_on': ['a']}])


def test_sbuild_log_link_is_not_a_dependency_input(tmp_path):
    root, lock, deb = bundle(tmp_path)
    (deb.parent / 'sample_amd64.build').symlink_to('/old/runner/sample_1.0-1_amd64.build')
    assert collect(root, lock)['sample'][0]['sha256'] == sha256(deb)
    malicious = deb.parent / 'redirect.deb'
    malicious.symlink_to('/outside/deb')
    with pytest.raises(ValueError, match='Symlink'):
        collect(root, lock)


def persist_lock(root, lock):
    (root / 'build-lock.json').write_text(json.dumps(lock))
    path = root / 'generation-manifest.json'
    manifest = json.loads(path.read_text())
    manifest['lock_sha256'] = lock_digest(lock)
    path.write_text(json.dumps(manifest))


def prepared_bundle(tmp_path):
    root, lock, deb = bundle(tmp_path)
    directory = deb.parent.parent / 'source'
    directory.mkdir()
    orig = directory / 'sample_1.0.orig.tar.gz'
    orig.write_bytes(b'fixture source payload')
    dsc = directory / 'sample_1.0-1.dsc'
    dsc.write_text(f'Source: sample\nVersion: 1.0-1\nChecksums-Sha256:\n {sha256(orig)} {orig.stat().st_size} {orig.name}\n')
    lock['catalog_entry'] = {'source': 'sample', 'upstream_sha': 'a' * 40}
    lock['packages'][0]['input'] = {'kind': 'prepared-snapshot', 'upstream_sha': 'a' * 40, 'dsc_sha256': sha256(dsc)}
    persist_lock(root, lock)
    return root, lock, orig, dsc


def test_prepared_handoff_binds_uploaded_source_to_frozen_upstream(tmp_path):
    root, lock, _, _ = prepared_bundle(tmp_path)
    assert collect(root, lock)['sample'][0]['version'] == '1.0-1'
    lock['packages'][0]['input']['upstream_sha'] = 'b' * 40
    persist_lock(root, lock)
    with pytest.raises(ValueError, match='frozen upstream'):
        collect(root, lock)


@pytest.mark.parametrize('corrupt', ['dsc', 'orig'])
def test_prepared_handoff_rehashes_source_payload(tmp_path, corrupt):
    root, lock, orig, dsc = prepared_bundle(tmp_path)
    (orig if corrupt == 'orig' else dsc).write_bytes(b'corrupt source')
    with pytest.raises(ValueError, match='checksum|corrupt'):
        collect(root, lock)


def test_dependency_buildinfo_cannot_silently_use_archive_version(tmp_path):
    root, lock, _ = bundle(tmp_path)
    lock['packages'][0]['required_build_versions'] = {'base-files': '2~snapshot'}
    persist_lock(root, lock)
    with pytest.raises(ValueError, match='required dependency'):
        collect(root, lock)


def test_producer_wrong_distribution_is_rejected(tmp_path):
    root, lock, deb = bundle(tmp_path)
    path = deb.parent / 'sample.changes'
    path.write_text(path.read_text().replace('Distribution: resolute', 'Distribution: noble'))
    with pytest.raises(ValueError, match='distribution mismatch'):
        collect(root, lock)
