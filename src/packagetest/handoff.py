"""Verify same-run artifacts before handing them to independent build/test jobs."""
from __future__ import annotations

from collections.abc import Iterator
import gzip
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from .artifacts import fields, sha256, verify_binaries, verify_source


def lock_digest(lock: dict) -> str:
    """Hash canonical lock JSON to bind uploaded artifacts to consumer expectations."""
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


@dataclass(frozen=True)
class ProducerBundle:
    """Downloaded generation metadata; trust comes from the consumer's lock."""

    manifest_path: Path
    manifest: dict[str, Any]

    def validate_generation(self, identity: dict[str, str], target: dict[str, Any],
                            relevant: set[str]) -> None:
        """Require a successful generation for the expected CI run and build target."""
        manifest_path, manifest = self.manifest_path, self.manifest
        if manifest.get('ci') != identity:
            raise ValueError(f'Stale or wrong-run producer: {manifest_path}')

        if manifest.get('result') != 'SUCCEEDED':
            raise ValueError(
                f'Producer build failed for {sorted(relevant)}: {manifest_path}')

        if manifest.get('target') != target:
            raise ValueError(
                f'Producer target mismatch for {sorted(relevant)}: '
                f'expected {target}, got {manifest.get("target")}: {manifest_path}')

    def validate_readiness(self, identity: dict[str, str], relevant: set[str]) -> None:
        """Require the producer readiness verdict to belong to this run and source set."""
        manifest_path = self.manifest_path
        readiness_path = manifest_path.parent / 'producer-readiness.json'
        if not readiness_path.is_file():
            raise ValueError(
                f'Missing producer build-and-test readiness for {sorted(relevant)}: '
                f'{readiness_path}')

        readiness = json.loads(readiness_path.read_text())
        if readiness.get('ci') != identity:
            raise ValueError(
                f'Stale or wrong-run producer readiness for {sorted(relevant)}: '
                f'{readiness_path}')

        if readiness.get('source') not in relevant:
            raise ValueError(
                f'Producer readiness source mismatch for {sorted(relevant)}: '
                f'{readiness_path}')

        # The readiness publisher owns test classification; consume its verdict.
        if readiness.get('result') != 'READY':
            raise ValueError(
                f'Producer validation failed for {sorted(relevant)}: '
                f'build={readiness.get("build_result")}, '
                f'autopkgtest={readiness.get("autopkgtest_result")}: '
                f'{readiness_path}')

    def validate_lock(self, source: str, expected: dict[str, Any],
                      saved_lock: dict[str, Any], target: dict[str, Any]) -> None:
        """Require agreement between the consumer lock, upload, and manifest digest."""
        if (saved_lock != expected or expected.get('target') != target
                or self.manifest.get('lock_sha256') != lock_digest(expected)):
            raise ValueError(f'Producer lock mismatch: {source}')

    def package_record(self, source: str, expected: dict[str, Any],
                       saved_lock: dict[str, Any], target: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Bind the uploaded record to the independently supplied package lock."""
        self.validate_lock(source, expected, saved_lock, target)
        package = _unique_package(expected['packages'], source)
        record = _unique_package(self.manifest['packages'], source)
        if record.get('result') != 'SUCCEEDED':
            raise ValueError(f'Invalid producer record: {source}')

        if record.get('version') != package['version']:
            raise ValueError(f'Producer version mismatch: {source}')

        return package, record

    def verify_package(self, source: str, expected: dict[str, Any],
                       saved_lock: dict[str, Any], target: dict[str, Any],
                       identity: dict[str, str]) -> list[dict[str, Any]]:
        """Verify locked package evidence and return binaries with local paths and provenance."""
        package, record = self.package_record(source, expected, saved_lock, target)
        directory = self.manifest_path.parent / 'artifacts' / source / 'binary'
        _validate_bundle_links(self.manifest_path.parent, source)
        _verify_prepared_source(source, package, expected, directory.parent / 'source')
        binaries = _verify_producer_binaries(source, package, record, directory, target)
        return [{**binary, 'path': str((directory / binary['file']).resolve()),
                 'source': source, 'producer_lock_sha256': lock_digest(expected),
                 'ci': identity} for binary in binaries]


def _unique_package(packages: list[dict[str, Any]], source: str) -> dict[str, Any]:
    """Resolve one source record without silently selecting a duplicate."""
    matches = [package for package in packages if package['source'] == source]
    if len(matches) != 1:
        raise ValueError(f'Invalid producer record: {source}')

    return matches[0]


def _validate_bundle_links(root: Path, source: str) -> None:
    # A downloaded archive must not redirect verification outside its bundle.
    # sbuild creates convenience .build log symlinks, often absolute.
    # They are never inputs to verification or installation. All other
    # symlinks remain forbidden, including artifact directories.
    """Reject bundle links that could redirect artifact validation outside the download."""
    if any(path.is_symlink() and not path.name.endswith('.build')
           for path in root.rglob('*')):
        raise ValueError(f'Symlink in producer bundle: {source}')


def _validate_frozen_upstream(source: str, acquisition: dict[str, Any],
                              catalog_entry: dict[str, Any]) -> None:
    """Bind prepared input identity to the independently frozen catalog entry."""
    if (catalog_entry.get('source') != source or not acquisition.get('upstream_sha')
            or acquisition['upstream_sha'] != catalog_entry.get('upstream_sha')):
        raise ValueError(f'Prepared snapshot differs from frozen upstream: {source}')


def _verify_prepared_source(source: str, package: dict[str, Any],
                            expected: dict[str, Any], source_dir: Path) -> None:
    """Verify prepared source identity, descriptor checksum, and referenced source bytes."""
    acquisition = package.get('input', {})
    if acquisition.get('kind') != 'prepared-snapshot':
        return

    _validate_frozen_upstream(source, acquisition, expected.get('catalog_entry', {}))
    dscs = list(source_dir.glob('*.dsc'))
    if len(dscs) != 1 or sha256(dscs[0]) != acquisition.get('dsc_sha256'):
        raise ValueError(f'Prepared source checksum mismatch: {source}')

    # A matching descriptor alone does not prove its referenced tarballs are intact.
    verify_source(dscs[0], source, package['version'])


def _verify_producer_binaries(source: str, package: dict[str, Any], record: dict[str, Any],
                              directory: Path, target: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify binary bytes and require their build metadata to match the locked producer."""
    verified = verify_binaries(directory, source=source, version=package['version'],
                               expected=package['expected_binaries'], arch=target['architecture'])
    distribution = target.get('distribution', target['suite'])
    if fields(directory / verified['changes']).get('Distribution') != distribution:
        raise ValueError(f'Producer binary distribution mismatch: {source}')

    for dependency, version in package.get('required_build_versions', {}).items():
        if verified['build_dependency_versions'].get(dependency) != version:
            raise ValueError(f'Producer did not use required dependency: {dependency}={version}')

    if verified['binaries'] != record.get('binaries'):
        raise ValueError(f'Producer binary manifest mismatch: {source}')

    return verified['binaries']


def _validated_bundles(download_root: Path, expected_locks: dict[str, dict[str, Any]],
                       identity: dict[str, str], target: dict[str, Any],
                       require_readiness: bool) -> Iterator[tuple[ProducerBundle, set[str], dict[str, Any]]]:
    """Discover relevant generations and validate them before exposing their locks."""
    for manifest_path in sorted(download_root.rglob('generation-manifest.json')):
        manifest = json.loads(manifest_path.read_text())
        sources = {package['source'] for package in manifest.get('packages', [])}
        relevant = sources & expected_locks.keys()
        if not relevant:
            continue

        bundle = ProducerBundle(manifest_path, manifest)
        bundle.validate_generation(identity, target, relevant)
        if require_readiness:
            bundle.validate_readiness(identity, relevant)

        saved_lock = json.loads((manifest_path.parent / 'build-lock.json').read_text())
        yield bundle, relevant, saved_lock


def collect_producers(download_root: Path, expected_locks: dict[str, dict[str, Any]], *,
                      run_id: str, run_attempt: str, target: dict[str, Any],
                      require_readiness: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Collect exactly one verified bundle per expected producer.

    Discovery ignores unrelated generations. Relevant generations must match
    the consumer's run, target, and locks before their bytes can be handed off.
    Paths are reconstructed from bundle layout, never uploaded absolute paths.
    """
    identity = {'run_id': str(run_id), 'run_attempt': str(run_attempt)}
    found: dict[str, list[dict[str, Any]]] = {}
    for bundle, relevant, saved_lock in _validated_bundles(
            download_root, expected_locks, identity, target, require_readiness):
        for source in relevant:
            if source in found:
                raise ValueError(f'Duplicate producer: {source}')

            found[source] = bundle.verify_package(
                source, expected_locks[source], saved_lock, target, identity)

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

                # Rehash the copy to detect changes between verification and handoff.
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
