"""Versioned build locks and a stage-based, fail-closed packaging executor."""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .artifacts import checksum_entries, fields, sha256, verify_binaries, verify_source
from .commands import CommandRunner


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hex(value: str, length: int) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(rf'[a-f0-9]{{{length}}}', value))


def load_lock(path: Path) -> dict:
    lock = json.loads(path.read_text())
    if lock.get('schema_version') != 1:
        raise ValueError('Unsupported lock schema_version (expected 1)')
    target = lock['target']
    for name in ('suite', 'architecture', 'chroot'):
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', target[name]):
            raise ValueError(f'Invalid target {name}')
    if target.get('backend') != 'schroot':
        raise ValueError('This slice requires an explicitly provisioned schroot backend')
    packages = lock['packages']
    if not packages:
        raise ValueError('Empty build lock')
    seen = set()
    for package in packages:
        source = package['source']
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', source) or source in seen:
            raise ValueError(f'Invalid or duplicate source: {source}')
        for dep in package.get('depends_on', []):
            if dep not in seen:
                raise ValueError(f'{source}: dependency {dep} must precede its consumer in lock')
        seen.add(source)
        for binary, version in package.get('required_build_versions', {}).items():
            if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+(?::[a-z0-9-]+)?', binary):
                raise ValueError('Invalid required binary dependency name')
            if subprocess.run(['dpkg', '--validate-version', version], capture_output=True).returncode:
                raise ValueError('Invalid required binary dependency version')
        if not package.get('expected_binaries'):
            raise ValueError(f'{source}: expected_binaries is required')
        if subprocess.run(['dpkg', '--validate-version', package['version']], capture_output=True).returncode:
            raise ValueError(f'{source}: invalid Debian version')
        acquisition = package['input']
        if acquisition['kind'] not in {'archive', 'gbp'}:
            raise ValueError('Only archive and gbp inputs are supported')
        snapshot = acquisition.get('snapshot')
        if snapshot:
            if acquisition['kind'] != 'gbp' or acquisition.get('import_mode') != 'new' or 'tarball' in acquisition:
                raise ValueError('Snapshot requires a new gbp import and no release tarball')
            if urlparse(snapshot['repository']).scheme != 'https':
                raise ValueError('Snapshot repository must use HTTPS')
            for key in ('sha', 'base_tag_sha'):
                if not _hex(snapshot[key], 40):
                    raise ValueError(f'Snapshot requires a full Git {key}')
            if not re.fullmatch(r'[0-9][a-zA-Z0-9.+~\-]*', snapshot['base_tag']):
                raise ValueError('Invalid snapshot base tag')
            if not _hex(snapshot.get('sdist_sha256'), 64):
                raise ValueError('Invalid snapshot sdist checksum')
            if not snapshot.get('build_requirements'):
                raise ValueError('Snapshot requires checksum-pinned build tools')
            for requirement in snapshot['build_requirements']:
                if (not re.fullmatch(r'[a-zA-Z0-9_.-]+', requirement['name'])
                        or not re.fullmatch(r'[0-9][a-zA-Z0-9.+!-]*', requirement['version'])
                        or not _hex(requirement['sha256'], 64)):
                    raise ValueError('Invalid snapshot build-tool pin')
        else:
            item = acquisition['dsc'] if acquisition['kind'] == 'archive' else acquisition['tarball']
            if urlparse(item['url']).scheme != 'https' or not _hex(item['sha256'], 64):
                raise ValueError(f'{source}: an HTTPS URL and SHA256 are required')
        if acquisition['kind'] == 'gbp':
            if acquisition.get('import_mode') not in {'reuse', 'new'}:
                raise ValueError('Explicit import_mode reuse or new is required')
            keys = ['packaging_sha', 'upstream_sha', 'pristine_tar_sha']
            if acquisition['import_mode'] == 'reuse':
                keys.append('upstream_tag_sha')
            for key in keys:
                if not _hex(acquisition[key], 40):
                    raise ValueError(f'{source}: pin a full Git SHA for {key}')
            if not re.fullmatch(r'[0-9][a-zA-Z0-9.+~\-]*', acquisition['upstream_version']):
                raise ValueError('Invalid Debian upstream version')
            for key in ('packaging_branch', 'upstream_branch', 'upstream_tag'):
                if subprocess.run(['git', 'check-ref-format', '--branch', acquisition[key]], capture_output=True).returncode:
                    raise ValueError(f'Invalid Git ref: {key}')
    from .targets import validate_target
    validate_target(lock)
    return lock


class StageFailure(RuntimeError):
    pass


class LockedBuild:
    def __init__(self, lock: dict, root: Path, *, timeout: float = 3600):
        self.lock = lock
        self.digest = hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.root = root.resolve() / f'gen-{self.digest[:12]}-{uuid.uuid4().hex[:8]}'
        self.root.mkdir(parents=True)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.runner = CommandRunner(self.root / 'logs' / 'commands.jsonl', stream=True, timeout=timeout)
        self.stage = 'preflight'
        self.checkout: Path | None = None
        self.last_result = None
        self.manifest = {'schema_version': 1, 'generation_id': self.root.name, 'lock_sha256': self.digest,
                         'target': lock['target'], 'started_at': _now(), 'packages': [],
                         'reproducibility': 'Source inputs pinned; archive dependency versions recorded in buildinfo'}
        (self.root / 'build-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
        self.save()

    def save(self):
        path = self.root / 'generation-manifest.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.manifest, indent=2) + '\n')
        temporary.replace(path)

    def command(self, *argv: str, cwd: Path | None = None, env: dict | None = None) -> str:
        self.last_result = self.runner.run(list(argv), cwd or self.work, env_diff=env)
        if self.last_result.exit_code:
            raise StageFailure(f'{self.stage}: {argv[0]} exited {self.last_result.exit_code}: {self.last_result.stderr[-2000:]}')
        return self.last_result.stdout.strip()

    def enter(self, stage: str):
        self.stage = stage
        self.last_result = None
        print(f'[{self.root.name}] {stage}', file=sys.stderr, flush=True)
        with (self.root / 'logs' / 'stages.jsonl').open('a') as handle:
            handle.write(json.dumps({'stage': stage, 'at': _now()}) + '\n')

    def download(self, item: dict, destination: Path):
        temporary = destination.with_suffix(destination.suffix + '.partial')
        try:
            with urlopen(item['url'], timeout=60) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            if sha256(temporary) != item['sha256']:
                raise StageFailure(f'Checksum mismatch: {item["url"]}')
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def failure(self, source: str, error: Exception):
        directory = self.root / 'failures' / source
        directory.mkdir(parents=True, exist_ok=True)
        files = {}
        if self.checkout and self.checkout.exists():
            for name in ('control', 'rules', 'changelog', 'gbp.conf', 'source/format', 'source/options', 'patches/series'):
                path = self.checkout / 'debian' / name
                if path.is_file():
                    files['debian/' + name] = path.read_text(errors='replace')
            patches = self.checkout / 'debian' / 'patches'
            if patches.is_dir():
                shutil.copytree(patches, directory / 'patches', dirs_exist_ok=True)
            if (self.checkout / '.git').exists():
                for args, name in ((['status', '--porcelain=v1'], 'git-status.txt'), (['diff', '--binary'], 'working-tree.patch'),
                                   (['log', '-5', '--oneline'], 'git-log.txt')):
                    result = subprocess.run(['git', '-C', str(self.checkout), *args], capture_output=True, text=True)
                    (directory / name).write_text(result.stdout + result.stderr)
        payload = {'source_package': source, 'generation_id': self.root.name, 'category': self.stage.upper().replace('-', '_'),
                   'error': str(error), 'files': files, 'lock_sha256': self.digest,
                   'command': self.last_result.to_dict() if self.last_result else None,
                   'failed_command': self.last_result.command if self.last_result else [],
                   'command_exit_code': self.last_result.exit_code if self.last_result else 1}
        (directory / 'failure.json').write_text(json.dumps(payload, indent=2) + '\n')
        (directory / 'analysis.md').write_text(f'# {source}: {self.stage}\n\n{error}\n\nSee failure.json, captured packaging files, and ../../logs/.\n')

    def preflight(self):
        self.enter('environment')
        target = self.lock['target']
        versions = {}
        for tool in ('dpkg', 'dpkg-source', 'dpkg-deb', 'sbuild', 'git', 'gbp', 'schroot', 'lintian'):
            versions[tool] = self.command(tool, '--version')
        versions['pristine-tar'] = self.command('dpkg-query', '-W', '-f=${Version}', 'pristine-tar')
        self.manifest['tools'] = versions
        self.manifest['builder_identity'] = self.command('id')
        self.manifest['kernel'] = self.command('uname', '-srmo')
        chroots = self.command('schroot', '--list').splitlines()
        if not any(line.removeprefix('chroot:') == target['chroot'] for line in chroots):
            raise StageFailure(f'Missing chroot {target["chroot"]}; run scripts/prepare-builder.sh')
        self.command('schroot', '-c', target['chroot'], '--directory', '/', '--', 'true')
        self.manifest['apt_sources'] = self.command('schroot', '-c', target['chroot'], '--directory', '/', '--',
            'sh', '-c', 'cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null; true')
        if target.get('profile') == 'noble-uca-epoxy':
            required = 'deb [signed-by=/usr/share/keyrings/ubuntu-cloud-keyring.gpg] http://ubuntu-cloud.archive.canonical.com/ubuntu noble-updates/epoxy main'
            if required not in self.manifest['apt_sources'] or 'trusted=yes' in self.manifest['apt_sources']:
                raise StageFailure('UCA chroot is missing its signed Epoxy repository')
        self.save()

    def archive_source(self, package: dict, source_dir: Path) -> Path:
        self.enter('source-acquisition')
        item = package['input']['dsc']
        filename = Path(urlparse(item['url']).path).name
        if not filename.endswith('.dsc'):
            raise ValueError('Expected .dsc URL')
        dsc = source_dir / filename
        self.download(item, dsc)
        for digest, size, name in checksum_entries(fields(dsc)):
            self.download({'url': urljoin(item['url'], name), 'sha256': digest}, source_dir / name)
        verify_source(dsc, package['source'], package['version'])
        self.checkout = self.work / package['source']
        self.command('dpkg-source', '--no-check', '-x', str(dsc), str(self.checkout))
        return dsc

    def git_source(self, package: dict, source_dir: Path) -> Path:
        self.enter('packaging-checkout')
        spec = package['input']
        checkout = self.work / package['source']
        self.checkout = checkout
        self.command('git', 'clone', '--no-checkout', spec['repository'], str(checkout))
        self.command('git', 'checkout', '-B', spec['packaging_branch'], spec['packaging_sha'], cwd=checkout)
        self.command('git', 'config', 'user.name', self.lock['maintainer']['name'], cwd=checkout)
        self.command('git', 'config', 'user.email', self.lock['maintainer']['email'], cwd=checkout)
        self.command('git', 'config', 'commit.gpgsign', 'false', cwd=checkout)
        self.command('git', 'config', 'tag.gpgsign', 'false', cwd=checkout)
        conf = configparser.ConfigParser(interpolation=None)
        conf.read(checkout / 'debian/gbp.conf')
        configured = conf.defaults()
        if configured.get('debian-branch', spec['packaging_branch']) != spec['packaging_branch']:
            raise StageFailure('Packaging branch conflicts with pinned gbp.conf')
        if configured.get('upstream-branch', 'upstream') != spec['upstream_branch']:
            raise StageFailure('Upstream branch conflicts with pinned gbp.conf')
        if fields(checkout / 'debian/control').get('Source') != package['source']:
            raise StageFailure('debian/control Source differs from lock')
        old_version = self.command('dpkg-parsechangelog', '-S', 'Version', cwd=checkout)
        old_source = self.command('dpkg-parsechangelog', '-S', 'Source', cwd=checkout)
        if old_source != package['source']:
            raise StageFailure('Changelog Source differs from lock')
        old_epoch = old_version.split(':')[0] if ':' in old_version else '0'
        new_epoch = package['version'].split(':')[0] if ':' in package['version'] else '0'
        if old_epoch != new_epoch:
            raise StageFailure('Epoch changes require a separate reviewed policy')
        backport = self.lock['target'].get('profile') == 'noble-uca-epoxy'
        if backport:
            if package.get('backport_of') != old_version:
                raise StageFailure('UCA backport base differs from pinned packaging changelog')
        else:
            self.command('dpkg', '--compare-versions', package['version'], 'gt', old_version)
        deb_upstream = package['version'].split(':')[-1].rsplit('-', 1)[0]
        if deb_upstream != spec['upstream_version']:
            raise StageFailure('Changelog and orig upstream versions differ')
        self.command('git', 'branch', '-f', spec['upstream_branch'], spec['upstream_sha'], cwd=checkout)
        self.command('git', 'branch', '-f', 'pristine-tar', spec['pristine_tar_sha'], cwd=checkout)
        self.enter('upstream-acquisition')
        tarball = source_dir / f'{package["source"]}_{spec["upstream_version"]}.orig.tar.gz'
        if 'snapshot' in spec:
            from .snapshot import build_snapshot
            self.enter('snapshot-sdist')
            self.manifest['packages'][-1]['snapshot'] = build_snapshot(self, package, tarball)
            self.save()
        else:
            self.download(spec['tarball'], tarball)
        self.enter('upstream-import')
        tag = spec['upstream_tag']
        existing = subprocess.run(['git', '-C', str(checkout), 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}'],
                                  text=True, capture_output=True)
        if (existing.returncode == 0) != (spec['import_mode'] == 'reuse'):
            raise StageFailure('Tag existence conflicts with explicit import_mode')
        if existing.returncode == 0:
            if existing.stdout.strip() != spec['upstream_tag_sha']:
                raise StageFailure('Upstream packaging tag has moved')
            pristine = self.work / 'pristine' / package['source']
            pristine.mkdir(parents=True)
            self.command('pristine-tar', 'checkout', str(pristine / tarball.name), cwd=checkout)
            if sha256(pristine / tarball.name) != spec['tarball']['sha256']:
                raise StageFailure('Existing pristine-tar import differs from pinned release tarball')
            self.command('git', 'branch', '-f', spec['upstream_branch'], spec['upstream_tag_sha'], cwd=checkout)
        else:
            # New imports are deliberately explicit; no overwriting of existing tags.
            self.command('gbp', 'import-orig', '--no-interactive', '--no-merge', '--pristine-tar',
                         f'--debian-branch={spec["packaging_branch"]}', f'--upstream-branch={spec["upstream_branch"]}',
                         f'--upstream-tag={tag}', f'--upstream-version={spec["upstream_version"]}', str(tarball), cwd=checkout)
        self.command('git', 'merge', '--no-edit', tag, cwd=checkout)
        self.enter('patch-validation')
        series = checkout / 'debian/patches/series'
        if series.is_file() and any(line.strip() and not line.lstrip().startswith('#') for line in series.read_text().splitlines()):
            self.command('gbp', 'pq', 'import', '--time-machine=0', cwd=checkout)
            self.command('git', 'checkout', spec['packaging_branch'], cwd=checkout)
            self.command('gbp', 'pq', 'drop', cwd=checkout)
        self.enter('changelog')
        env = {'DEBFULLNAME': self.lock['maintainer']['name'], 'DEBEMAIL': self.lock['maintainer']['email']}
        self.command('dch', *(['--force-bad-version'] if backport else []), '--distribution', self.lock['target']['suite'], '--force-distribution', '--newversion',
                     package['version'], f'Build pinned upstream release {spec["upstream_version"]} for packaging validation.', cwd=checkout, env=env)
        self.command('git', 'add', 'debian/changelog', cwd=checkout)
        self.command('git', 'commit', '-m', f'New upstream release {spec["upstream_version"]}', cwd=checkout)
        if self.command('git', 'status', '--porcelain', cwd=checkout):
            raise StageFailure('Packaging checkout is not clean before source build')
        self.enter('source-build')
        export_dir = self.work / 'export' / package['source']
        export_dir.mkdir(parents=True)
        self.command('gbp', 'buildpackage', f'--git-debian-branch={spec["packaging_branch"]}',
                     f'--git-upstream-branch={spec["upstream_branch"]}', '--git-pristine-tar',
                     f'--git-upstream-tag={spec["upstream_tag"]}',
                     f'--git-export-dir={export_dir}', '--git-builder=dpkg-buildpackage -S -d -nc -us -uc', cwd=checkout, env=env)
        candidates = list(export_dir.glob('*.dsc'))
        if len(candidates) != 1:
            raise StageFailure(f'Expected one source .dsc in {export_dir}, found {len(candidates)}')
        dsc = candidates[0]
        verify_source(dsc, package['source'], package['version'])
        for pattern in ('*.dsc', '*.orig.tar.*', '*.debian.tar.*', '*.changes', '*.buildinfo'):
            for path in export_dir.glob(pattern):
                shutil.copy2(path, source_dir / path.name)
        self.manifest['packages'][-1]['packaging_result_sha'] = self.command('git', 'rev-parse', 'HEAD', cwd=checkout)
        self.command('git', 'diff', spec['packaging_sha'], '--', 'debian', cwd=checkout)
        patch = subprocess.check_output(['git', '-C', str(checkout), 'diff', spec['packaging_sha'], '--', 'debian'])
        (source_dir.parent / 'packaging.patch').write_bytes(patch)
        return source_dir / dsc.name

    def run(self) -> int:
        try:
            self.preflight()
        except Exception as exc:
            self.failure('environment', exc)
            self.manifest.update(result='FAILED', error=str(exc), finished_at=_now())
            self.save()
            return 1
        completed = {}
        failed = set()
        for package in self.lock['packages']:
            source = package['source']
            record = {'source': source, 'version': package['version'], 'started_at': _now(), 'result': 'BUILDING'}
            self.manifest['packages'].append(record)
            self.checkout = None
            if any(dep in failed for dep in package.get('depends_on', [])):
                record.update(result='BLOCKED', blocked_by=[dep for dep in package.get('depends_on', []) if dep in failed], finished_at=_now())
                failed.add(source)
                self.save()
                continue
            output = self.root / 'artifacts' / source
            source_dir, binary_dir = output / 'source', output / 'binary'
            source_dir.mkdir(parents=True)
            binary_dir.mkdir()
            try:
                dsc = self.archive_source(package, source_dir) if package['input']['kind'] == 'archive' else self.git_source(package, source_dir)
                record['source_artifacts'] = [{'file': path.name, 'sha256': sha256(path)}
                                              for path in sorted(source_dir.iterdir()) if path.is_file()]
                self.enter('binary-build')
                target = self.lock['target']
                argv = ['sbuild', '--verbose', f'--chroot-mode={target["backend"]}', f'--chroot={target["chroot"]}',
                        f'--dist={target["suite"]}', f'--arch={target["architecture"]}', '--arch-all',
                        f'--build-dir={binary_dir}', '--no-run-lintian']
                record['dependency_artifacts'] = []
                for dep in package.get('depends_on', []):
                    for artifact in completed[dep]:
                        path = artifact['path']
                        if sha256(path) != artifact['sha256']:
                            raise StageFailure(f'Dependency artifact changed after validation: {path}')
                        argv.append(f'--extra-package={path}')
                        record['dependency_artifacts'].append({**artifact, 'path': str(path), 'source': dep})
                for binary, version in package.get('required_build_versions', {}).items():
                    argv.append(f'--add-depends={binary} (= {version})')
                self.save()
                self.command(*argv, str(dsc), cwd=binary_dir)
                self.enter('artifact-validation')
                validation = verify_binaries(binary_dir, source=source, version=package['version'],
                                             expected=package['expected_binaries'], arch=target['architecture'])
                for dep, version in package.get('required_build_versions', {}).items():
                    if validation['build_dependency_versions'].get(dep) != version:
                        raise StageFailure(f'Build did not use required dependency {dep}={version}')
                self.enter('lintian')
                self.command('lintian', '--fail-on=error', str(binary_dir / validation['changes']), cwd=binary_dir)
                record.update(validation, result='SUCCEEDED', finished_at=_now())
                completed[source] = [{**b, 'path': binary_dir / b['file']} for b in validation['binaries']]
            except Exception as exc:
                failed.add(source)
                self.failure(source, exc)
                record.update(result='FAILED', failed_stage=self.stage, error=str(exc), finished_at=_now())
            self.save()
        self.manifest.update(result='FAILED' if failed else 'SUCCEEDED', finished_at=_now())
        self.save()
        print(json.dumps({'result': self.manifest['result'], 'run_dir': str(self.root), 'lock_sha256': self.digest}))
        return int(bool(failed))
