import json
import subprocess
from pathlib import Path

import pytest
from packagetest.artifacts import sha256, verify_binaries
from packagetest.locked import load_lock, LockedBuild, StageFailure

LOCK = Path(__file__).resolve().parents[1] / 'config/locks/oslo-i18n-noble-baseline.json'


def test_lock_rejects_unpinned_inputs(tmp_path):
    data = json.loads(LOCK.read_text())
    data['packages'][0]['input']['dsc']['sha256'] = 'latest'
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='SHA256'):
        load_lock(path)


def test_missing_artifacts_never_succeed(tmp_path, monkeypatch):
    build = LockedBuild(load_lock(LOCK), tmp_path)
    monkeypatch.setattr(build, 'preflight', lambda: None)
    monkeypatch.setattr(build, 'archive_source', lambda package, directory: directory / 'missing.dsc')
    monkeypatch.setattr(build, 'command', lambda *args, **kwargs: '')
    assert build.run() == 1
    manifest = json.loads((build.root / 'generation-manifest.json').read_text())
    assert manifest['packages'][0]['failed_stage'] == 'artifact-validation'
    assert (build.root / 'failures/python-oslo.i18n/failure.json').exists()


def test_failed_dependency_blocks_consumer(tmp_path, monkeypatch):
    lock = load_lock(LOCK)
    lock['packages'].append(dict(lock['packages'][0], source='consumer', depends_on=['python-oslo.i18n']))
    build = LockedBuild(lock, tmp_path)
    monkeypatch.setattr(build, 'preflight', lambda: None)
    def fail(*args):
        raise StageFailure('source unavailable')
    monkeypatch.setattr(build, 'archive_source', fail)
    assert build.run() == 1
    assert [p['result'] for p in build.manifest['packages']] == ['FAILED', 'BLOCKED']


@pytest.fixture
def binary_output(tmp_path):
    tree = tmp_path / 'tree'
    (tree / 'DEBIAN').mkdir(parents=True)
    (tree / 'DEBIAN/control').write_text('Package: python3-example\nSource: example\nVersion: 1.0-1\nArchitecture: all\nMaintainer: Test <test@example.invalid>\nDescription: fixture\n')
    output = tmp_path / 'output'
    output.mkdir()
    deb = output / 'python3-example_1.0-1_all.deb'
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(tree), str(deb)], check=True, capture_output=True)
    info = output / 'example_1.0-1_amd64.buildinfo'
    info.write_text('Source: example\nVersion: 1.0-1\nInstalled-Build-Depends: dpkg-dev (= 1.22.6)\n')
    changes = output / 'example_1.0-1_amd64.changes'
    changes.write_text('Source: example\nVersion: 1.0-1\nChecksums-Sha256:\n' + ''.join(f' {sha256(p)} {p.stat().st_size} {p.name}\n' for p in (deb, info)))
    return output


def validate(output):
    return verify_binaries(output, source='example', version='1.0-1', expected=['python3-example'], arch='amd64')


def test_actual_deb_metadata_and_buildinfo(binary_output):
    result = validate(binary_output)
    assert result['binaries'][0]['package'] == 'python3-example'
    assert result['build_dependency_versions']['dpkg-dev'] == '1.22.6'


def test_corrupt_deb_rejected(binary_output):
    next(binary_output.glob('*.deb')).write_bytes(b'not a deb')
    with pytest.raises(ValueError, match='corrupt'):
        validate(binary_output)


def test_wrong_expected_binary_rejected(binary_output):
    with pytest.raises(ValueError, match='Expected binaries'):
        verify_binaries(binary_output, source='example', version='1.0-1', expected=['missing'], arch='amd64')


def dependency_build(tmp_path, monkeypatch, installed_version):
    import copy
    lock = load_lock(LOCK)
    producer = lock['packages'][0]
    producer.update(source='producer', version='1.0-1+local1', expected_binaries=['python3-producer'])
    consumer = copy.deepcopy(producer)
    consumer.update(source='consumer', depends_on=['producer'], required_build_versions={'python3-producer': '1.0-1+local1'})
    lock['packages'].append(consumer)
    build = LockedBuild(lock, tmp_path)
    monkeypatch.setattr(build, 'preflight', lambda: None)
    monkeypatch.setattr(build, 'archive_source', lambda package, directory: directory / 'input.dsc')
    calls = []
    def command(*argv, **kwargs):
        calls.append(argv)
        return ''
    monkeypatch.setattr(build, 'command', command)
    def verify(directory, **kwargs):
        binary = directory / 'output.deb'
        binary.write_bytes(b'validated fixture output')
        return {'binaries': [{'file': binary.name, 'package': 'python3-producer', 'version': '1.0-1+local1', 'sha256': sha256(binary)}],
                'changes': 'output.changes', 'build_dependency_versions': {'python3-producer': installed_version}}
    monkeypatch.setattr('packagetest.locked.verify_binaries', verify)
    return build, calls


def test_consumer_receives_artifact_and_exact_solver_constraint(tmp_path, monkeypatch):
    build, calls = dependency_build(tmp_path, monkeypatch, '1.0-1+local1')
    assert build.run() == 0
    consumer = [c for c in calls if c[0] == 'sbuild'][1]
    assert '--add-depends=python3-producer (= 1.0-1+local1)' in consumer
    assert any(a.startswith('--extra-package=') and '/producer/binary/' in a for a in consumer)
    evidence = build.manifest['packages'][1]['dependency_artifacts'][0]
    assert evidence['source'] == 'producer'
    assert evidence['sha256'] == sha256(Path(evidence['path']))


def test_consumer_cannot_succeed_using_archive_version(tmp_path, monkeypatch):
    build, _ = dependency_build(tmp_path, monkeypatch, '1.0-1')
    assert build.run() == 1
    record = build.manifest['packages'][1]
    assert record['result'] == 'FAILED'
    assert 'did not use required dependency' in record['error']
