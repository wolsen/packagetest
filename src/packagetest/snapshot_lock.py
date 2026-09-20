"""Resolve mutable upstream discovery once into a checksum-pinned build lock."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from .commands import CommandRunner
from .locked import load_lock
from .snapshot import build_snapshot, snapshot_version


class Resolver:
    def __init__(self, root: Path):
        self.root = root.resolve() / ('resolve-' + uuid.uuid4().hex[:12])
        self.work = self.root / 'work'
        self.work.mkdir(parents=True)
        self.runner = CommandRunner(self.root / 'logs' / 'commands.jsonl', stream=True, timeout=900)

    def command(self, *args, cwd=None, env=None):
        result = self.runner.run(list(args), cwd or self.work, env_diff=env)
        if result.exit_code:
            raise RuntimeError(f'{args[0]} failed: {result.stderr[-2000:]}')
        return result.stdout.strip()


def select_commit(resolver, checkout: Path, ref: str, cutoff: str | None) -> dict:
    resolver.command('git', 'check-ref-format', '--branch', ref)
    tip = resolver.command('git', 'rev-parse', f'refs/remotes/origin/{ref}^{{commit}}', cwd=checkout)
    sha = tip
    if cutoff:
        when = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
        if when.tzinfo is None:
            raise ValueError('Snapshot cutoff requires an explicit timezone')
        limit = int(when.timestamp())
        # Enumerate instead of --before: Git traversal may stop at a timestamp
        # reversal. Only first-parent history defines the selected branch state.
        history = resolver.command('git', 'log', '--first-parent', '--format=%H %ct', tip, cwd=checkout)
        sha = next((line.split()[0] for line in history.splitlines() if int(line.split()[1]) <= limit), None)
        if sha is None:
            raise ValueError('No branch commit exists at or before cutoff')
    base = resolver.command('git', 'describe', '--tags', '--abbrev=0', '--match=[0-9]*', sha, cwd=checkout)
    if not re.fullmatch(r'[0-9][a-zA-Z0-9.+~\-]*', base):
        raise ValueError('Snapshot base is not a supported release tag')
    base_sha = resolver.command('git', 'rev-parse', f'refs/tags/{base}^{{commit}}', cwd=checkout)
    count = int(resolver.command('git', 'rev-list', '--count', f'{base_sha}..{sha}', cwd=checkout))
    epoch = int(resolver.command('git', 'show', '-s', '--format=%ct', sha, cwd=checkout))
    version = snapshot_version(base, epoch, count, sha)
    return {'ref': ref, 'resolved_branch_tip': tip, 'cutoff': cutoff, 'sha': sha,
            'resolved_at': datetime.now(timezone.utc).isoformat(), 'base_tag': base,
            'base_tag_sha': base_sha, 'commits_since_tag': count, 'commit_timestamp': str(epoch),
            'pep440_version': version.replace('~', '.'), 'upstream_version': version}


def create_snapshot_lock(template: Path, output: Path, root: Path, *, ref='master', cutoff=None) -> dict:
    if output.exists():
        raise ValueError(f'Refusing to overwrite existing lock: {output}')
    lock = deepcopy(load_lock(template))
    if len(lock['packages']) != 1 or not lock['packages'][0]['input'].get('snapshot'):
        raise ValueError('Snapshot resolution requires a single-package snapshot template')
    package = lock['packages'][0]
    acquisition = package['input']
    resolver = Resolver(root)
    checkout = resolver.work / 'discovery'
    resolver.command('git', 'clone', '--no-checkout', acquisition['snapshot']['repository'], str(checkout))
    selected = select_commit(resolver, checkout, ref, cutoff)
    version = selected.pop('upstream_version')
    old_version = package['version']
    epoch = old_version.split(':', 1)[0] + ':' if ':' in old_version else ''
    revision = old_version.rsplit('-', 1)[1]
    package['version'] = f'{epoch}{version}-{revision}'
    acquisition.update(upstream_version=version, upstream_tag=version, import_mode='new')
    acquisition.pop('upstream_tag_sha', None)
    acquisition.pop('tarball', None)
    acquisition['snapshot'].update(selected)
    acquisition['snapshot'].pop('sdist_sha256', None)
    result = build_snapshot(resolver, package, resolver.root / f'{package["source"]}_{version}.orig.tar.gz')
    acquisition['snapshot']['sdist_sha256'] = result['sdist_sha256']
    candidate = resolver.root / 'build-lock.json'
    candidate.write_text(json.dumps(lock, indent=2) + '\n')
    load_lock(candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids silently replacing a previously reviewed lock.
    with output.open('x') as handle:
        handle.write(candidate.read_text())
    report = {'result': 'LOCKED', 'lock': str(output.resolve()), 'run_dir': str(resolver.root), 'snapshot': result}
    (resolver.root / 'resolution.json').write_text(json.dumps(report, indent=2) + '\n')
    return report
