import json
import subprocess

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
    (root / 'producer-readiness.json').write_text(json.dumps({
        'schema_version': 1, 'source': 'sample', 'result': 'READY',
        'build_result': 'SUCCEEDED', 'autopkgtest_result': 'PASS',
        'ci': {'run_id': '123', 'run_attempt': '1'},
    }))
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


def test_failed_producer_and_target_mismatch_are_distinguished(tmp_path):
    root, lock, _ = bundle(tmp_path)
    path = root / 'generation-manifest.json'
    manifest = json.loads(path.read_text())
    manifest['result'] = 'FAILED'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"Producer build failed for \['sample'\]"):
        collect(root, lock)

    manifest['result'] = 'SUCCEEDED'
    manifest['target']['suite'] = 'noble'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"Producer target mismatch for \['sample'\]"):
        collect(root, lock)


def test_rejects_producer_whose_autopkgtest_failed(tmp_path):
    root, lock, _ = bundle(tmp_path)
    readiness = json.loads((root / 'producer-readiness.json').read_text())
    readiness.update(result='FAILED', autopkgtest_result='FAIL')
    (root / 'producer-readiness.json').write_text(json.dumps(readiness))

    with pytest.raises(
            ValueError,
            match=r"Producer validation failed for \['sample'\]: build=SUCCEEDED, autopkgtest=FAIL"):
        collect(root, lock)


@pytest.mark.parametrize('change', ['missing', 'run', 'source'])
def test_rejects_invalid_producer_readiness(tmp_path, change):
    root, lock, _ = bundle(tmp_path)
    path = root / 'producer-readiness.json'
    readiness = json.loads(path.read_text())
    if change == 'missing':
        path.unlink()
    elif change == 'run':
        readiness['ci']['run_id'] = '122'
        path.write_text(json.dumps(readiness))
    else:
        readiness['source'] = 'other'
        path.write_text(json.dumps(readiness))
    with pytest.raises(ValueError, match='producer .*readiness|readiness source mismatch'):
        collect(root, lock)


def test_candidate_can_be_tested_before_readiness_then_used_downstream(tmp_path):
    root, lock, _ = bundle(tmp_path)
    readiness = root / 'producer-readiness.json'
    readiness.unlink()

    # The package under test has no test verdict yet, but its complete build
    # manifest and artifacts still receive all other handoff validation.
    candidate = collect(root, lock, require_readiness=False)
    assert set(candidate) == {'sample'}
    with pytest.raises(ValueError, match='Missing producer build-and-test readiness'):
        collect(root, lock)

    readiness.write_text(json.dumps({
        'schema_version': 1, 'source': 'sample', 'result': 'READY',
        'build_result': 'SUCCEEDED', 'autopkgtest_result': 'PASS',
        'ci': {'run_id': '123', 'run_attempt': '1'},
    }))
    assert set(collect(root, lock)) == {'sample'}


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


def test_handoff_preserves_complete_provenance(tmp_path):
    root, lock, deb = bundle(tmp_path)
    assert collect(root, lock) == {'sample': [{
        'file': deb.name, 'package': 'sample', 'version': '1.0-1',
        'architecture': 'all', 'sha256': sha256(deb), 'path': str(deb.resolve()),
        'source': 'sample', 'producer_lock_sha256': lock_digest(lock),
        'ci': {'run_id': '123', 'run_attempt': '1'},
    }]}


def test_unrelated_generation_does_not_require_matching_run_or_readiness(tmp_path):
    root, lock, _ = bundle(tmp_path)
    unrelated = root.parent / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'generation-manifest.json').write_text(json.dumps({
        'packages': [{'source': 'other'}], 'result': 'FAILED',
        'ci': {'run_id': 'old', 'run_attempt': '9'},
    }))
    assert set(collect(root, lock)) == {'sample'}


@pytest.mark.parametrize('result', ['FAILED', 'BLOCKED', 'MISSING'])
def test_readiness_requires_ready_verdict(tmp_path, result):
    root, lock, _ = bundle(tmp_path)
    path = root / 'producer-readiness.json'
    readiness = json.loads(path.read_text())
    readiness['result'] = result
    path.write_text(json.dumps(readiness))
    with pytest.raises(ValueError, match='Producer validation failed'):
        collect(root, lock)


def test_readiness_from_previous_attempt_is_rejected(tmp_path):
    root, lock, _ = bundle(tmp_path)
    path = root / 'producer-readiness.json'
    readiness = json.loads(path.read_text())
    readiness['ci']['run_attempt'] = '0'
    path.write_text(json.dumps(readiness))
    with pytest.raises(ValueError, match='wrong-run producer readiness'):
        collect(root, lock)


@pytest.mark.parametrize('change, message', [
    ('duplicate', 'Invalid producer record'),
    ('failed', 'Invalid producer record'),
    ('version', 'Producer version mismatch'),
])
def test_package_record_must_be_unique_successful_and_exact(tmp_path, change, message):
    root, lock, _ = bundle(tmp_path)
    path = root / 'generation-manifest.json'
    manifest = json.loads(path.read_text())
    if change == 'duplicate':
        manifest['packages'].append(manifest['packages'][0].copy())
    elif change == 'failed':
        manifest['packages'][0]['result'] = 'FAILED'
    else:
        manifest['packages'][0]['version'] = '2.0-1'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=message):
        collect(root, lock)


def test_saved_lock_must_equal_independent_lock_even_when_digest_matches(tmp_path):
    root, lock, _ = bundle(tmp_path)
    path = root / 'build-lock.json'
    saved = json.loads(path.read_text())
    saved['packages'][0]['version'] = '2.0-1'
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='Producer lock mismatch'):
        collect(root, lock)


def test_deferred_readiness_does_not_skip_byte_verification(tmp_path):
    root, lock, deb = bundle(tmp_path)
    (root / 'producer-readiness.json').unlink()
    deb.write_bytes(b'changed')
    with pytest.raises(ValueError):
        collect(root, lock, require_readiness=False)


@pytest.mark.parametrize('change', ['missing', 'multiple'])
def test_prepared_handoff_requires_exactly_one_dsc(tmp_path, change):
    root, lock, _, dsc = prepared_bundle(tmp_path)
    if change == 'missing':
        dsc.unlink()
    else:
        (dsc.parent / 'extra.dsc').write_bytes(dsc.read_bytes())
    with pytest.raises(ValueError, match='Prepared source checksum mismatch'):
        collect(root, lock)


def test_artifact_directory_symlink_is_rejected(tmp_path):
    root, lock, deb = bundle(tmp_path)
    original = deb.parent
    outside = tmp_path / 'outside'
    original.rename(outside)
    original.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink in producer bundle'):
        collect(root, lock)


def test_repository_index_failure_removes_partial_repository(tmp_path, monkeypatch):
    """Failed index generation must not leave copied packages in a consumable repository."""
    root, lock, _ = bundle(tmp_path)
    producers = collect(root, lock)
    destination = tmp_path / 'repository'

    def fail_index(command, **kwargs):
        """Simulate failure after verified binaries have already been copied."""
        assert (destination / 'pool/sample_1.0-1_all.deb').is_file()
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr('packagetest.handoff.subprocess.check_output', fail_index)
    with pytest.raises(subprocess.CalledProcessError):
        build_repository(producers, destination)

    assert not destination.exists()


def test_repository_refuses_existing_destination_without_deleting_it(tmp_path):
    """A failed reservation must preserve the caller's existing directory and files."""
    destination = tmp_path / 'repository'
    destination.mkdir()
    sentinel = destination / 'keep'
    sentinel.write_text('existing data')
    with pytest.raises(ValueError, match='destination already exists'):
        build_repository({}, destination)

    assert sentinel.read_text() == 'existing data'


def test_repository_refuses_duplicate_binary_identity_and_cleans_up(tmp_path):
    """Two producers cannot supply the same package and architecture to one testbed."""
    root, lock, _ = bundle(tmp_path)
    producers = collect(root, lock)
    producers['other'] = producers['sample']
    destination = tmp_path / 'repository'
    with pytest.raises(ValueError, match='Duplicate binary package'):
        build_repository(producers, destination)

    assert not destination.exists()


@pytest.mark.packaging_component
def test_consolidated_producer_archive_preserves_verified_handoff(tmp_path):
    """A minimal published bundle still verifies binary bytes and frozen source components."""
    from pathlib import Path
    import runpy
    import tarfile

    root, lock, deb = bundle(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    generation = outputs / 'gen-sample'
    root.rename(generation)
    source = generation / 'artifacts/sample/source'
    source.mkdir()
    component = source / 'sample_1.0.orig.tar.gz'
    component.write_bytes(b'pinned source component')
    dsc = source / 'sample_1.0-1.dsc'
    dsc.write_text('Source: sample\nVersion: 1.0-1\nChecksums-Sha256:\n'
                   f' {sha256(component)} {component.stat().st_size} {component.name}\n')
    lock['catalog_entry'] = {'source': 'sample', 'upstream_sha': 'a' * 40}
    lock['packages'][0]['input'] = {'kind': 'prepared-snapshot', 'dsc_sha256': sha256(dsc),
                                  'upstream_sha': 'a' * 40}
    (generation / 'build-lock.json').write_text(json.dumps(lock))
    manifest = json.loads((generation / 'generation-manifest.json').read_text())
    manifest['lock_sha256'] = lock_digest(lock)
    (generation / 'generation-manifest.json').write_text(json.dumps(manifest))
    (outputs / 'result.json').write_text('{"source":"sample","result":"SUCCEEDED"}')
    tests = tmp_path / 'test-results'
    (tests / 'result').mkdir(parents=True)
    (tests / 'result/result.json').write_text('{"source":"sample","result":"PASS"}')
    publisher = runpy.run_path(str(Path('scripts/publish-package-artifacts.py').resolve()))['PackageArtifacts']
    publication = tmp_path / 'publication'
    publisher('sample', outputs, tests, tmp_path / 'no-remediation', publication).publish()
    downloaded = tmp_path / 'consumer-inputs'
    downloaded.mkdir()
    with tarfile.open(publication / 'producer/sample.tar.gz') as archive:
        archive.extractall(downloaded, filter='data')

    producers = collect_producers(downloaded, {'sample': lock}, run_id='123', run_attempt='1', target=lock['target'])
    assert producers['sample'][0]['sha256'] == sha256(generation / 'artifacts/sample/binary' / deb.name)
    component = downloaded / 'gen-sample/artifacts/sample/source' / component.name
    component.write_bytes(b'corrupt source component')
    with pytest.raises(ValueError, match='corrupt artifact'):
        collect_producers(downloaded, {'sample': lock}, run_id='123', run_attempt='1', target=lock['target'])
