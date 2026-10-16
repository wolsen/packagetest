"""Generate a PBR source distribution from a pinned, full-history Git checkout."""
from __future__ import annotations

import gzip
import configparser
import io
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class SnapshotBuilder(Protocol):
    """Workspace and checked command boundary needed to construct a snapshot."""

    work: Path

    def command(self, *argv: str, cwd: Path | None = None, env: dict | None = None) -> str:
        """Run a command and return its complete output or raise on failure."""
        ...


def snapshot_version(base: str, timestamp: int, count: int, sha: str) -> str:
    if count < 0:
        raise ValueError('Snapshot commit count cannot be negative')
    from .versioning import upstream_version_to_debian_version
    debian_base = upstream_version_to_debian_version(base).rsplit('-', 1)[0]
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y%m%d')
    return f'{debian_base}+git{date}.{count}.{sha[:7]}'



def snapshot_pep440_version(version: str) -> str:
    """Translate Debian prerelease ordering to canonical Python metadata."""
    normalized = re.sub(r'~(?=(?:a|b|rc)[0-9])', '', version)
    if '~' in normalized:
        raise ValueError('Unsupported Debian prerelease marker for Python metadata')
    if '+' in normalized:
        public, local = normalized.split('+', 1)
        # PEP 440 treats an all-numeric local-version component as an
        # integer.  setuptools therefore writes ``0037426`` as ``37426`` in
        # PKG-INFO.  Store that same canonical spelling in the snapshot lock.
        components = re.split(r'[._-]', local)
        local = '.'.join(str(int(component)) if component.isdigit() else component.lower()
                         for component in components)
        normalized = f'{public}+{local}'
    return normalized

def _sdist_metadata(archive: tarfile.TarFile, members: list[tarfile.TarInfo]) -> str:
    """Read the first root-relative PKG-INFO after required-name validation."""
    root_info = next(m for m in members if m.name.split('/', 1)[-1] == 'PKG-INFO')
    metadata = archive.extractfile(root_info).read().decode()
    return metadata


def _validate_sdist_metadata(archive: tarfile.TarFile, members: list[tarfile.TarInfo], required_metadata: tuple[str, ...] | list[str], version: str) -> None:
    """Require generated metadata and the exact locked Python version."""
    names = {m.name.split('/', 1)[-1] for m in members}
    if 'PKG-INFO' not in required_metadata or not set(required_metadata).issubset(names):
        raise ValueError('Snapshot sdist lacks PBR-generated metadata')

    metadata = _sdist_metadata(archive, members)
    if not re.search(r'^Version: ' + re.escape(version) + r'$', metadata, re.M):
        raise ValueError('Snapshot PKG-INFO version differs from lock')


def _validate_sdist_member(member: tarfile.TarInfo) -> None:
    """Reject absolute/traversing paths and non-file archive objects."""
    if member.name.startswith('/') or '..' in Path(member.name).parts or not (member.isfile() or member.isdir()):
        raise ValueError('Unexpected snapshot archive member')


def _normalize_sdist_header(member: tarfile.TarInfo, epoch: int, archive_format: str) -> None:
    """Normalize ownership, timestamps, and portable permissions without changing legacy modes."""
    if archive_format == 'portable-v1':
        member.mode = 0o755 if member.isdir() or member.mode & 0o100 else 0o644

    member.uid = member.gid = 0
    member.uname = member.gname = ''
    member.mtime = epoch
    member.pax_headers = {}


def _write_sdist_member(archive: tarfile.TarFile, target: tarfile.TarFile,
                        member: tarfile.TarInfo, epoch: int, archive_format: str) -> None:
    """Validate one member and copy its data with deterministic archive headers."""
    _validate_sdist_member(member)
    data = archive.extractfile(member).read() if member.isfile() else None
    _normalize_sdist_header(member, epoch, archive_format)
    target.addfile(member, io.BytesIO(data) if data is not None else None)


def _snapshot_checkout(build: SnapshotBuilder, package: dict) -> tuple[Path, str, str]:
    """Checkout and validate the pinned upstream commit and release ancestry."""
    spec = package['input']['snapshot']
    upstream = build.work / 'snapshot' / package['source']
    upstream.parent.mkdir(parents=True, exist_ok=True)
    build.command('git', 'clone', '--no-checkout', spec['repository'], str(upstream))
    build.command('git', 'checkout', '--detach', spec['sha'], cwd=upstream)
    actual_sha = build.command('git', 'rev-parse', 'HEAD', cwd=upstream)
    base_sha = build.command('git', 'rev-parse', f'refs/tags/{spec["base_tag"]}^{{commit}}', cwd=upstream)
    if actual_sha != spec['sha'] or base_sha != spec['base_tag_sha']:
        raise ValueError('Snapshot Git pins differ from checkout')

    build.command('git', 'merge-base', '--is-ancestor', base_sha, actual_sha, cwd=upstream)
    return upstream, actual_sha, base_sha


def _validate_snapshot_history(spec: dict, package: dict, count: int, commits: list[dict], epoch: int, version: str) -> None:
    """Require the observed history, timestamp, and Debian version to match the lock."""
    if (count != spec['commits_since_tag'] or commits != spec.get('commits', commits)
            or epoch != int(spec['commit_timestamp'])
            or version != package['input']['upstream_version']):
        raise ValueError('Snapshot date/count/version differs from lock')


def _snapshot_history(build: SnapshotBuilder, package: dict, upstream: Path, actual_sha: str, base_sha: str) -> tuple[int, str]:
    """Inventory release-relative history and validate both version representations."""
    spec = package['input']['snapshot']
    count = int(build.command('git', 'rev-list', '--count', f'{base_sha}..{actual_sha}', cwd=upstream))
    history = build.command('git', 'log', '--reverse', '--format=%H%x00%ct%x00%an%x00%s',
                            f'{base_sha}..{actual_sha}', cwd=upstream)
    commits = [{'sha': fields[0], 'timestamp': int(fields[1]), 'author': fields[2],
                'subject': fields[3]}
               for line in history.splitlines() for fields in [line.split('\0', 3)]]
    epoch = int(build.command('git', 'show', '-s', '--format=%ct', actual_sha, cwd=upstream))
    version = snapshot_version(spec['base_tag'], epoch, count, actual_sha)
    _validate_snapshot_history(spec, package, count, commits, epoch, version)
    if spec['pep440_version'] != snapshot_pep440_version(version):
        raise ValueError('Snapshot PEP 440 version differs from Debian upstream version')

    return epoch, version


def _snapshot_environment(spec: dict, upstream: Path, epoch: int) -> tuple[dict[str, str], str]:
    """Validate the version backend and set reproducible build environment variables."""
    env = {'PBR_VERSION': spec['pep440_version'], 'SOURCE_DATE_EPOCH': str(epoch), 'TZ': 'UTC'}
    version_backend = spec.get('version_backend', 'pbr')
    if version_backend == 'setuptools-scm':
        import tomllib
        project = tomllib.loads((upstream / 'pyproject.toml').read_text())
        if 'setuptools_scm' not in project.get('tool', {}):
            raise ValueError('Source does not declare the pinned setuptools-scm backend')

        env['SETUPTOOLS_SCM_PRETEND_VERSION'] = spec['pep440_version']
    elif version_backend != 'pbr':
        raise ValueError('Unsupported snapshot version backend')

    return env, version_backend


def _generate_sdist(build: SnapshotBuilder, upstream: Path, venv: Path, dist: Path, env: dict[str, str]) -> None:
    """Invoke the declared source backend through the pinned build environment."""
    if (upstream / 'setup.py').exists():
        build.command(str(venv / 'bin/python'), 'setup.py', 'sdist', f'--dist-dir={dist}', cwd=upstream, env=env)
    else:
        import tomllib
        project = tomllib.loads((upstream / 'pyproject.toml').read_text())
        backend = project['build-system']['build-backend']
        if backend not in {'pbr.build', 'setuptools.build_meta'}:
            raise ValueError('Unpinned source build backend: ' + backend)

        build.command(str(venv / 'bin/python'), '-c',
                      'import importlib,sys; importlib.import_module(sys.argv[1]).build_sdist(sys.argv[2])',
                      backend, str(dist), cwd=upstream, env=env)


def _required_sdist_metadata(upstream: Path, version_backend: str) -> list[str]:
    """Honor explicit PBR metadata omissions while always requiring PKG-INFO."""
    required_metadata = ['AUTHORS', 'ChangeLog', 'PKG-INFO'] if version_backend == 'pbr' else ['PKG-INFO']
    config = configparser.ConfigParser(interpolation=None)
    config.read(upstream / 'setup.cfg')
    for option, filename in [('skip_changelog', 'ChangeLog'), ('skip_authors', 'AUTHORS')]:
        if filename in required_metadata and config.getboolean('pbr', option, fallback=False):
            required_metadata.remove(filename)

    return required_metadata


def canonical_sdist(source: Path, destination: Path, *, epoch: int, version: str, archive_format: str = "portable-v1",
                    required_metadata: tuple[str, ...] | list[str] = ('AUTHORS', 'ChangeLog', 'PKG-INFO')) -> None:
    """Keep generated sdist contents; normalize archive headers for repeatability."""
    if archive_format not in {'legacy', 'portable-v1'}:
        raise ValueError('Unsupported snapshot archive format')

    with tarfile.open(source, 'r:gz') as archive:
        members = archive.getmembers()
        _validate_sdist_metadata(archive, members, required_metadata, version)
        with destination.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target:
                for member in sorted(members, key=lambda m: m.name):
                    _write_sdist_member(archive, target, member, epoch, archive_format)


def build_snapshot(build: SnapshotBuilder, package: dict, destination: Path) -> dict:
    """Build and verify a reproducible sdist from checksum-pinned tools and Git history."""
    from .artifacts import sha256
    spec = package['input']['snapshot']
    upstream, actual_sha, base_sha = _snapshot_checkout(build, package)
    epoch, version = _snapshot_history(build, package, upstream, actual_sha, base_sha)
    venv = upstream.parent / 'venv'
    build.command('python3', '-m', 'venv', str(venv))
    requirements = upstream.parent / 'requirements.txt'
    requirements.write_text(''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in spec['build_requirements']))
    build.command(str(venv / 'bin/pip'), '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
                  '--only-binary=:all:', '--require-hashes', '-r', str(requirements))
    dist = upstream.parent / 'dist'
    dist.mkdir()
    env, version_backend = _snapshot_environment(spec, upstream, epoch)
    _generate_sdist(build, upstream, venv, dist, env)
    archives = list(dist.glob('*.tar.gz'))
    if len(archives) != 1:
        raise ValueError('Expected exactly one generated snapshot sdist')

    required_metadata = _required_sdist_metadata(upstream, version_backend)
    canonical_sdist(archives[0], destination, epoch=epoch, version=spec['pep440_version'],
                    archive_format=spec.get('archive_format', 'legacy'), required_metadata=required_metadata)
    digest = sha256(destination)
    if spec.get('sdist_sha256') and digest != spec['sdist_sha256']:
        raise ValueError(f'Generated snapshot sdist checksum differs from lock: expected {spec["sdist_sha256"]}, got {digest}')

    return {**spec, 'sdist_sha256': digest, 'sdist_file': destination.name, 'required_metadata': required_metadata,
            'python': build.command(str(venv / 'bin/python'), '--version'),
            'installed_build_tools': build.command(str(venv / 'bin/pip'), 'freeze')}
