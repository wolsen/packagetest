import os
import subprocess
from pathlib import Path
import pytest
from packagetest.snapshot_lock import Resolver, select_commit, create_snapshot_lock


def git(repo, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_COMMITTER_NAME='Test',
               GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_EMAIL='test@example.invalid')
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env, text=True).strip()


def history(tmp_path):
    repo = tmp_path / 'git'
    repo.mkdir()
    git(repo, 'init', '-b', 'master')
    git(repo, 'commit', '--allow-empty', '-m', 'release', date='2026-01-01T12:00:00Z')
    git(repo, 'tag', '1.0.0')
    git(repo, 'commit', '--allow-empty', '-m', 'first', date='2026-01-02T12:00:00Z')
    first = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'commit', '--allow-empty', '-m', 'second', date='2026-01-04T12:00:00Z')
    tip = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/remotes/origin/master', tip)
    return repo, first, tip


def test_select_branch_and_cutoff(tmp_path):
    repo, first, tip = history(tmp_path)
    resolver = Resolver(tmp_path / 'runs')
    latest = select_commit(resolver, repo, 'master', None)
    earlier = select_commit(resolver, repo, 'master', '2026-01-03T00:00:00Z')
    assert latest['sha'] == tip and latest['commits_since_tag'] == 2
    assert earlier['sha'] == first and earlier['commits_since_tag'] == 1
    assert earlier['resolved_branch_tip'] == tip
    assert earlier['upstream_version'].startswith('1.0.0+git20260102.1.')
    with pytest.raises(ValueError, match='timezone'):
        select_commit(resolver, repo, 'master', '2026-01-03')
    with pytest.raises(ValueError, match='No branch commit'):
        select_commit(resolver, repo, 'master', '2025-01-03T00:00:00Z')
    with pytest.raises(ValueError, match='after its base tag'):
        select_commit(resolver, repo, 'master', '2026-01-01T23:00:00Z')


def test_existing_reviewed_lock_never_overwritten(tmp_path):
    output = tmp_path / 'lock.json'
    output.write_text('reviewed')
    with pytest.raises(ValueError, match='overwrite'):
        create_snapshot_lock(Path('missing'), output, tmp_path)
    assert output.read_text() == 'reviewed'


def test_resolution_preserves_packaging_pins_and_only_writes_after_sdist(tmp_path, monkeypatch):
    import json
    import packagetest.snapshot_lock as module
    template = Path(__file__).resolve().parents[1] / 'config/locks/oslo-i18n-stonking-snapshot.json'
    original = json.loads(template.read_text())
    spec = original['packages'][0]['input']['snapshot']
    selection = {key: spec[key] for key in ('ref', 'sha', 'base_tag', 'base_tag_sha', 'commits_since_tag', 'commit_timestamp', 'pep440_version')}
    selection['upstream_version'] = original['packages'][0]['input']['upstream_version']
    monkeypatch.setattr(module.Resolver, 'command', lambda *args, **kwargs: '')
    monkeypatch.setattr(module, 'select_commit', lambda *args: dict(selection))
    output = tmp_path / 'output.json'
    def fail(*args):
        raise ValueError('sdist failed')
    monkeypatch.setattr(module, 'build_snapshot', fail)
    with pytest.raises(ValueError, match='sdist failed'):
        create_snapshot_lock(template, output, tmp_path / 'runs')
    assert not output.exists()
    monkeypatch.setattr(module, 'build_snapshot', lambda *args: {'sdist_sha256': 'a' * 64})
    create_snapshot_lock(template, output, tmp_path / 'runs')
    actual = json.loads(output.read_text())
    assert actual['target'] == original['target']
    for key in ('packaging_sha', 'upstream_sha', 'pristine_tar_sha', 'packaging_branch'):
        assert actual['packages'][0]['input'][key] == original['packages'][0]['input'][key]
    assert json.loads(template.read_text()) == original
