"""Generate a PBR source distribution from a pinned, full-history Git checkout."""
from __future__ import annotations

import gzip
import io
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def snapshot_version(base: str, timestamp: int, count: int, sha: str) -> str:
    if count < 1:
        raise ValueError('Snapshot must be after its base tag; use a release lock at a tag')
    from .versioning import upstream_version_to_debian_version
    debian_base = upstream_version_to_debian_version(base).rsplit('-', 1)[0]
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y%m%d')
    return f'{debian_base}+git{date}.{count}.{sha[:7]}'


def canonical_sdist(source: Path, destination: Path, *, epoch: int, version: str):
    """Keep generated sdist contents; normalize archive headers for repeatability."""
    with tarfile.open(source, 'r:gz') as archive:
        members = archive.getmembers()
        names = {m.name.split('/', 1)[-1] for m in members}
        if not {'AUTHORS', 'ChangeLog', 'PKG-INFO'}.issubset(names):
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
    if spec['pep440_version'] != version.replace('~', '.'):
        raise ValueError('Snapshot PEP 440 version differs from Debian upstream version')
    venv = upstream.parent / 'venv'
    build.command('python3', '-m', 'venv', str(venv))
    requirements = upstream.parent / 'requirements.txt'
    requirements.write_text(''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in spec['build_requirements']))
    build.command(str(venv / 'bin/pip'), '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
                  '--only-binary=:all:', '--require-hashes', '-r', str(requirements))
    dist = upstream.parent / 'dist'
    dist.mkdir()
    build.command(str(venv / 'bin/python'), 'setup.py', 'sdist', f'--dist-dir={dist}', cwd=upstream,
                  env={'PBR_VERSION': spec['pep440_version'], 'SOURCE_DATE_EPOCH': str(epoch), 'TZ': 'UTC'})
    archives = list(dist.glob('*.tar.gz'))
    if len(archives) != 1:
        raise ValueError('Expected exactly one generated snapshot sdist')
    canonical_sdist(archives[0], destination, epoch=epoch, version=spec['pep440_version'])
    digest = sha256(destination)
    if spec.get('sdist_sha256') and digest != spec['sdist_sha256']:
        raise ValueError('Generated snapshot sdist checksum differs from lock')
    return {**spec, 'sdist_sha256': digest, 'sdist_file': destination.name,
            'python': build.command(str(venv / 'bin/python'), '--version'),
            'installed_build_tools': build.command(str(venv / 'bin/pip'), 'freeze')}
