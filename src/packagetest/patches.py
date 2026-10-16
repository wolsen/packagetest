"""Quilt patch evidence and policy for frozen upstream sources."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

from .artifacts import sha256


def already_applied_patches(tree: Path) -> list[dict]:
    """Omit a quilt patch only if its complete inverse applies without fuzz.

    Also require that the forward patch does not apply, so ambiguous repeated
    contexts do not qualify. Both probes are dry runs; upstream is unchanged.
    """
    series = tree / 'debian/patches/series'
    if not series.exists():
        return []
    lines = series.read_text().splitlines()
    omitted = []
    for index, line in enumerate(lines):
        words = line.split()
        if not words or words[0].startswith('#') or len(words) != 1:
            continue
        name = Path(words[0])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe quilt patch path')
        patch = tree / 'debian/patches' / name
        data = patch.read_bytes()
        args = ['patch', '--dry-run', '--force', '--fuzz=0', '-p1']
        reverse = subprocess.run(args + ['--reverse'], input=data, cwd=tree, capture_output=True)
        if reverse.returncode:
            continue
        forward = subprocess.run(args, input=data, cwd=tree, capture_output=True)
        if forward.returncode:
            lines[index] = '# Fully present upstream (verified reverse dry run): ' + str(name)
            omitted.append({'action': 'omit-already-applied-patch', 'name': str(name),
                            'sha256': sha256(patch), 'reverse_probe': reverse.stdout.decode(errors='replace')})
    if omitted:
        series.write_text('\n'.join(lines) + '\n')
    return omitted


def integrated_upstream_patches(tree: Path, checkout: Path,
                                upstream_sha: str) -> list[dict]:
    """Omit forwarded patches whose Gerrit change is in frozen upstream.

    Textual patch probes become ambiguous after upstream evolves the merged
    implementation. A Gerrit Change-Id in both the Debian patch header and an
    ancestor commit identifies the forwarded change independently of later
    edits to the same files.
    """
    if not re.fullmatch(r'[0-9a-f]{40}', upstream_sha):
        raise ValueError('Integrated-patch check requires a frozen upstream SHA')
    series = tree / 'debian/patches/series'
    if not series.is_file():
        return []
    lines = series.read_text().splitlines()
    omitted = []
    for index, line in enumerate(lines):
        words = line.split()
        if not words or words[0].startswith('#'):
            continue
        name = Path(words[0])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe quilt patch path')
        patch = tree / 'debian/patches' / name
        if not patch.is_file() or patch.is_symlink():
            continue
        text = patch.read_text(errors='replace')
        first_section = re.search(r'(?m)^(?:diff --git |---\s)', text)
        header = text[:first_section.start()] if first_section else text
        change_ids = set(re.findall(
            r'(?m)^Change-Id:\s*(I[0-9a-fA-F]{40})\s*$', header))
        if len(change_ids) != 1:
            continue
        change_id = next(iter(change_ids))
        result = subprocess.run(
            ['git', 'log', upstream_sha, '-n', '1', '--format=%H',
             '--fixed-strings', '--grep=' + f'Change-Id: {change_id}'],
            cwd=checkout, text=True, capture_output=True, timeout=30, check=True)
        commit = result.stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            continue
        lines[index] = (f'# Integrated upstream as {commit[:12]} '
                        f'({change_id}): {line}')
        omitted.append({
            'action': 'omit-integrated-upstream-patch',
            'name': str(name),
            'sha256': sha256(patch),
            'change_id': change_id,
            'upstream_commit': commit,
            'human_review_required': True,
        })
    if omitted:
        series.write_text('\n'.join(lines) + '\n')
    return omitted


SEMANTIC_PATCH_STOPWORDS = {
    'false', 'from', 'import', 'none', 'return', 'self', 'test', 'true',
    'value', 'with',
}


def _semantic_patch_tokens(value: str) -> set[str]:
    result = set()
    for token in re.findall(r'[A-Za-z][A-Za-z0-9_]{3,}', value):
        token = token.lower()
        if token not in SEMANTIC_PATCH_STOPWORDS:
            result.add(token)
        result.update(part.lower() for part in token.split('_')
                      if len(part) >= 4 and part.lower() not in SEMANTIC_PATCH_STOPWORDS)
    return result


def superseded_upstream_patches(tree: Path, checkout: Path, selected: dict) -> list[dict]:
    """Suggest upstream commits for review without disabling a quilt patch.

    Identifier overlap is a search heuristic, never proof of equivalent
    behavior. Keep the patch active so source validation still fails closed.
    """
    series = tree / 'debian/patches/series'
    if not series.is_file():
        return []
    lines = series.read_text().splitlines()
    adjustments = []
    commits = [item for item in selected.get('commits', [])
               if re.fullmatch(r'[0-9a-f]{40}', item.get('sha', ''))]
    if not commits:
        return []
    for line in lines:
        words = line.split()
        if len(words) != 1 or words[0].startswith('#'):
            continue
        name = Path(words[0])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe quilt patch path')
        patch = tree / 'debian/patches' / name
        if not patch.is_file() or patch.is_symlink():
            continue
        data = patch.read_bytes()
        probe = ['patch', '--dry-run', '--batch', '--force', '--fuzz=0', '-p1']
        if subprocess.run(probe, input=data, cwd=tree, capture_output=True).returncode == 0:
            continue
        if subprocess.run(probe + ['--reverse'], input=data, cwd=tree,
                          capture_output=True).returncode == 0:
            continue
        text = data.decode(errors='replace')
        targets = []
        for target in re.findall(r'^\+\+\+\s+(?:b/)?([^\t\n ]+)', text, re.M):
            path = Path(target)
            if path.is_absolute() or '..' in path.parts or path.parts[:1] == ('debian',):
                targets = []
                break
            if target not in targets:
                targets.append(target)
        added = '\n'.join(item[1:] for item in text.splitlines()
                          if item.startswith('+') and not item.startswith('+++'))
        tokens = _semantic_patch_tokens(added)
        definitions = set(re.findall(
            r'\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)', added))
        if not targets or len(tokens) < 8 or not definitions:
            continue
        current_parts = []
        for target in targets:
            path = tree / target
            if not path.is_file() or path.is_symlink():
                current_parts = []
                break
            current_parts.append(path.read_text(errors='replace'))
        if not current_parts or not all(any(definition in part for part in current_parts)
                                        for definition in definitions):
            continue
        current_tokens = _semantic_patch_tokens('\n'.join(current_parts))
        current_overlap = len(tokens & current_tokens) / len(tokens)
        if current_overlap < 0.70:
            continue
        best = None
        for commit in commits:
            result = subprocess.run(
                ['git', 'show', '--format=%B', commit['sha'], '--', *targets],
                cwd=checkout, text=True, capture_output=True, timeout=30)
            if result.returncode or not result.stdout.strip():
                continue
            commit_tokens = _semantic_patch_tokens(result.stdout)
            overlap = len(tokens & commit_tokens) / len(tokens)
            if overlap >= 0.70 and (best is None or overlap > best['commit_overlap']):
                best = {'sha': commit['sha'], 'subject': commit.get('subject', ''),
                        'commit_overlap': round(overlap, 3)}
        if best is None:
            continue
        adjustments.append({
            'action': 'review-upstream-patch-overlap', 'name': str(name),
            'sha256': sha256(patch), 'upstream_commit': best,
            'current_overlap': round(current_overlap, 3),
            'token_count': len(tokens), 'targets': targets,
            'human_review_required': True,
        })
    return adjustments

