"""Resolve mutable upstream discovery once into a checksum-pinned build lock."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from .commands import CommandExecutor, CommandRunner
from .locked import load_lock
from .snapshot import build_snapshot, snapshot_version, snapshot_pep440_version


class Resolver:
    """Own an upstream-discovery workspace and its injected process boundary."""

    def __init__(self, root: Path, *, runner: CommandExecutor | None = None) -> None:
        """Create a fresh discovery workspace with complete command capture."""
        self.root = root.resolve() / ('resolve-' + uuid.uuid4().hex[:12])
        self.work = self.root / 'work'
        self.work.mkdir(parents=True)
        self.runner: CommandExecutor = runner if runner is not None else CommandRunner(
            self.root / 'logs' / 'commands.jsonl', stream=True, timeout=900)

    def command(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> str:
        """Return complete command stdout for history and metadata discovery."""
        result = self.runner.run(list(args), cwd or self.work, env_diff=env)
        if result.exit_code:
            raise RuntimeError(f'{args[0]} failed: {result.stderr[-2000:]}')

        return result.read_stdout().strip()


def _commit_at_cutoff(resolver: Resolver, checkout: Path, tip: str, cutoff: str | None) -> str:
    """Select from first-parent history without assuming monotonic commit dates."""
    if not cutoff:
        return tip

    when = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
    if when.tzinfo is None:
        raise ValueError('Snapshot cutoff requires an explicit timezone')

    history = resolver.command('git', 'log', '--first-parent', '--format=%H %ct', tip, cwd=checkout)
    return _first_commit_before(history, int(when.timestamp()))


def _first_commit_before(history: str, limit: int) -> str:
    """Choose the first eligible branch state, allowing timestamp reversals."""
    for line in history.splitlines():
        sha, timestamp = line.split()
        if int(timestamp) <= limit:
            return sha

    raise ValueError('No branch commit exists at or before cutoff')


def select_commit(resolver: Resolver, checkout: Path, ref: str, cutoff: str | None) -> dict:
    """Resolve the branch state and record its complete release-relative commit inventory."""
    resolver.command('git', 'check-ref-format', '--branch', ref)
    tip = resolver.command('git', 'rev-parse', f'refs/remotes/origin/{ref}^{{commit}}', cwd=checkout)
    sha = _commit_at_cutoff(resolver, checkout, tip, cutoff)
    base = resolver.command('git', 'describe', '--tags', '--abbrev=0', '--match=[0-9]*', sha, cwd=checkout)
    if not re.fullmatch(r'[0-9][a-zA-Z0-9.+~\-]*', base):
        raise ValueError('Snapshot base is not a supported release tag')

    base_sha = resolver.command('git', 'rev-parse', f'refs/tags/{base}^{{commit}}', cwd=checkout)
    count = int(resolver.command('git', 'rev-list', '--count', f'{base_sha}..{sha}', cwd=checkout))
    history = resolver.command(
        'git', 'log', '--reverse', '--format=%H%x00%ct%x00%an%x00%s',
        f'{base_sha}..{sha}', cwd=checkout)
    commits = []
    for line in history.splitlines():
        commit_sha, timestamp, author, subject = line.split('\0', 3)
        commits.append({'sha': commit_sha, 'timestamp': int(timestamp),
                        'author': author, 'subject': subject})

    if len(commits) != count:
        raise ValueError('Snapshot commit inventory differs from Git revision count')

    epoch = int(resolver.command('git', 'show', '-s', '--format=%ct', sha, cwd=checkout))
    version = snapshot_version(base, epoch, count, sha)
    return {'ref': ref, 'resolved_branch_tip': tip, 'cutoff': cutoff, 'sha': sha,
            'resolved_at': datetime.now(timezone.utc).isoformat(), 'base_tag': base,
            'base_tag_sha': base_sha, 'commits_since_tag': count, 'commits': commits,
            'commit_timestamp': str(epoch),
            'pep440_version': snapshot_pep440_version(version), 'upstream_version': version}


def create_snapshot_lock(template: Path, output: Path, root: Path, *, ref='master', cutoff=None) -> dict:
    """Resolve a single-package template and exclusively publish its validated immutable lock."""
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
    acquisition['snapshot']['archive_format'] = 'portable-v1'
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
