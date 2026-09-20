"""Verify same-run artifacts before handing them to independent build/test jobs."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .artifacts import sha256, verify_binaries


def lock_digest(lock: dict) -> str:
    return hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def dependency_waves(packages: list[dict]) -> list[list[str]]:
    """Return maximum-width topological waves, rejecting incomplete graphs."""
    graph = {p['source']: set(p.get('depends_on', [])) for p in packages}
    if len(graph) != len(packages):
        raise ValueError('Duplicate source in dependency graph')
    for source, deps in graph.items():
        if deps - graph.keys():
            raise ValueError(f'{source}: missing producers {sorted(deps - graph.keys())}')
    waves, completed = [], set()
    while len(completed) < len(graph):
        wave = sorted(source for source, deps in graph.items() if source not in completed and deps <= completed)
        if not wave:
            raise ValueError(f'Dependency cycle: {sorted(graph.keys() - completed)}')
        waves.append(wave)
        completed.update(wave)
    return waves


def stamp_generation(manifest_path: Path, *, run_id: str, run_attempt: str) -> dict:
    """Attach CI identity before upload; the receiving job supplies expected identity."""
    if not str(run_id) or not str(run_attempt):
        raise ValueError('Missing CI run identity')
    manifest = json.loads(manifest_path.read_text())
    identity = {'run_id': str(run_id), 'run_attempt': str(run_attempt)}
    if manifest.get('ci', identity) != identity:
        raise ValueError('Generation already belongs to another CI run')
    manifest['ci'] = identity
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def collect_producers(download_root: Path, expected_locks: dict[str, dict], *,
                      run_id: str, run_attempt: str, target: dict) -> dict[str, list[dict]]:
    """Validate downloaded producer bundles against independently supplied locks.

    Uploaded directory layout must preserve generation-manifest.json alongside
    build-lock.json and artifacts/<source>/binary/. No absolute uploaded paths
    are trusted. Every expected producer must appear exactly once.
    """
    identity = {'run_id': str(run_id), 'run_attempt': str(run_attempt)}
    found = {}
    for manifest_path in sorted(download_root.rglob('generation-manifest.json')):
        manifest = json.loads(manifest_path.read_text())
        sources = {p['source'] for p in manifest.get('packages', [])}
        relevant = sources & expected_locks.keys()
        if not relevant:
            continue
        if manifest.get('ci') != identity:
            raise ValueError(f'Stale or wrong-run producer: {manifest_path}')
        if manifest.get('result') != 'SUCCEEDED' or manifest.get('target') != target:
            raise ValueError(f'Failed producer or wrong target: {manifest_path}')
        saved_lock = json.loads((manifest_path.parent / 'build-lock.json').read_text())
        for source in relevant:
            if source in found:
                raise ValueError(f'Duplicate producer: {source}')
            expected = expected_locks[source]
            if saved_lock != expected or manifest.get('lock_sha256') != lock_digest(expected):
                raise ValueError(f'Producer lock mismatch: {source}')
            packages = [p for p in expected['packages'] if p['source'] == source]
            records = [p for p in manifest['packages'] if p['source'] == source]
            if len(packages) != 1 or len(records) != 1 or records[0].get('result') != 'SUCCEEDED':
                raise ValueError(f'Invalid producer record: {source}')
            package, record = packages[0], records[0]
            if record.get('version') != package['version']:
                raise ValueError(f'Producer version mismatch: {source}')
            directory = manifest_path.parent / 'artifacts' / source / 'binary'
            # A downloaded archive must not redirect verification outside its bundle.
            if any(path.is_symlink() for path in manifest_path.parent.rglob('*')):
                raise ValueError(f'Symlink in producer bundle: {source}')
            verified = verify_binaries(directory, source=source, version=package['version'],
                                       expected=package['expected_binaries'], arch=target['architecture'])
            if verified['binaries'] != record.get('binaries'):
                raise ValueError(f'Producer binary manifest mismatch: {source}')
            found[source] = [{**binary, 'path': str((directory / binary['file']).resolve()),
                              'source': source, 'producer_lock_sha256': lock_digest(expected),
                              'ci': identity} for binary in verified['binaries']]
    missing = expected_locks.keys() - found.keys()
    if missing:
        raise ValueError(f'Missing successful producers: {sorted(missing)}')
    return found


def build_repository(producers: dict[str, list[dict]], destination: Path) -> dict:
    """Reconstruct a flat APT repo from verified handoffs, rechecking copied bytes.

    This unsigned repository is restricted to a disposable testbed. Its provenance
    binds packages to the pipeline run; it must never be used as a published archive.
    """
    if destination.exists():
        raise ValueError(f'Repository destination already exists: {destination}')
    destination.mkdir(parents=True)
    pool = destination / 'pool'
    pool.mkdir()
    records, identities = [], set()
    try:
        for source, binaries in sorted(producers.items()):
            for binary in binaries:
                path = Path(binary['path'])
                if path.is_symlink() or not path.is_file() or sha256(path) != binary['sha256']:
                    raise ValueError(f'Dependency artifact changed: {path}')
                identity = (binary['package'], binary['architecture'])
                if identity in identities:
                    raise ValueError(f'Duplicate binary package: {identity}')
                identities.add(identity)
                if path.name != binary['file'] or not path.name.endswith('.deb'):
                    raise ValueError(f'Invalid binary path: {path}')
                output = pool / path.name
                if output.exists():
                    raise ValueError(f'Conflicting binary filename: {path.name}')
                shutil.copyfile(path, output)
                if sha256(output) != binary['sha256']:
                    raise ValueError(f'Copied binary checksum mismatch: {path.name}')
                records.append({**binary, 'source': source, 'path': f'pool/{path.name}'})
        if not records:
            raise ValueError('Cannot reconstruct an empty package repository')
        packages = subprocess.check_output(['dpkg-scanpackages', '--multiversion', 'pool', '/dev/null'], cwd=destination)
        (destination / 'Packages').write_bytes(packages)
        (destination / 'Packages.gz').write_bytes(gzip.compress(packages, mtime=0))
        release = 'Origin: PackagetestRun\nLabel: PackagetestRun\nDescription: Verified same-run candidate packages\nSHA256:\n'
        for name in ('Packages', 'Packages.gz'):
            path = destination / name
            release += f' {sha256(path)} {path.stat().st_size} {name}\n'
        (destination / 'Release').write_text(release)
        result = {'schema_version': 1, 'packages': records,
                  'apt_source': f'deb [trusted=yes] file:{destination.resolve()} ./',
                  'trust_scope': 'verified artifacts in disposable testbed only'}
        (destination / 'provenance.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    except Exception:
        shutil.rmtree(destination)
        raise
