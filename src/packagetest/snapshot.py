"""Generate a PBR source distribution from a pinned, full-history Git checkout."""
from __future__ import annotations

import gzip
import configparser
import io
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path


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
    return normalized

def canonical_sdist(source: Path, destination: Path, *, epoch: int, version: str, archive_format: str = "portable-v1",
                    required_metadata=('AUTHORS', 'ChangeLog', 'PKG-INFO')):
    """Keep generated sdist contents; normalize archive headers for repeatability."""
    if archive_format not in {'legacy', 'portable-v1'}:
        raise ValueError('Unsupported snapshot archive format')
    with tarfile.open(source, 'r:gz') as archive:
        members = archive.getmembers()
        names = {m.name.split('/', 1)[-1] for m in members}
        if 'PKG-INFO' not in required_metadata or not set(required_metadata).issubset(names):
            raise ValueError('Snapshot sdist lacks PBR-generated metadata')
        root_info = next(m for m in members if m.name.split('/', 1)[-1] == 'PKG-INFO')
        metadata = archive.extractfile(root_info).read().decode()
        if not re.search(r'^Version: ' + re.escape(version) + r'$', metadata, re.M):
            raise ValueError('Snapshot PKG-INFO version differs from lock')
        with destination.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target:
                for member in sorted(members, key=lambda m: m.name):
                    if member.name.startswith('/') or '..' in Path(member.name).parts or not (member.isfile() or member.isdir()):
                        raise ValueError('Unexpected snapshot archive member')
                    data = archive.extractfile(member).read() if member.isfile() else None
                    if archive_format == 'portable-v1':
                        member.mode = 0o755 if member.isdir() or member.mode & 0o100 else 0o644
                    member.uid = member.gid = 0
                    member.uname = member.gname = ''
                    member.mtime = epoch
                    member.pax_headers = {}
                    target.addfile(member, io.BytesIO(data) if data is not None else None)


def build_snapshot(build, package: dict, destination: Path) -> dict:
    from .artifacts import sha256
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
    count = int(build.command('git', 'rev-list', '--count', f'{base_sha}..{actual_sha}', cwd=upstream))
    epoch = int(build.command('git', 'show', '-s', '--format=%ct', actual_sha, cwd=upstream))
    version = snapshot_version(spec['base_tag'], epoch, count, actual_sha)
    if count != spec['commits_since_tag'] or epoch != int(spec['commit_timestamp']) or version != package['input']['upstream_version']:
        raise ValueError('Snapshot date/count/version differs from lock')
    if spec['pep440_version'] != snapshot_pep440_version(version):
        raise ValueError('Snapshot PEP 440 version differs from Debian upstream version')
    venv = upstream.parent / 'venv'
    build.command('python3', '-m', 'venv', str(venv))
    requirements = upstream.parent / 'requirements.txt'
    requirements.write_text(''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in spec['build_requirements']))
    build.command(str(venv / 'bin/pip'), '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
                  '--only-binary=:all:', '--require-hashes', '-r', str(requirements))
    dist = upstream.parent / 'dist'
    dist.mkdir()
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
    archives = list(dist.glob('*.tar.gz'))
    if len(archives) != 1:
        raise ValueError('Expected exactly one generated snapshot sdist')
    required_metadata = ['AUTHORS', 'ChangeLog', 'PKG-INFO'] if version_backend == 'pbr' else ['PKG-INFO']
    config = configparser.ConfigParser(interpolation=None)
    config.read(upstream / 'setup.cfg')
    for option, filename in [('skip_changelog', 'ChangeLog'), ('skip_authors', 'AUTHORS')]:
        if filename in required_metadata and config.getboolean('pbr', option, fallback=False):
            required_metadata.remove(filename)
    canonical_sdist(archives[0], destination, epoch=epoch, version=spec['pep440_version'],
                    archive_format=spec.get('archive_format', 'legacy'), required_metadata=required_metadata)
    digest = sha256(destination)
    if spec.get('sdist_sha256') and digest != spec['sdist_sha256']:
        raise ValueError(f'Generated snapshot sdist checksum differs from lock: expected {spec["sdist_sha256"]}, got {digest}')
    return {**spec, 'sdist_sha256': digest, 'sdist_file': destination.name, 'required_metadata': required_metadata,
            'python': build.command(str(venv / 'bin/python'), '--version'),
            'installed_build_tools': build.command(str(venv / 'bin/pip'), 'freeze')}
