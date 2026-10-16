"""Quilt patch evidence and policy for frozen upstream sources."""
from __future__ import annotations

from dataclasses import dataclass
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


def _semantic_token_parts(token: str) -> set[str]:
    """Retain meaningful identifier components for advisory overlap scoring."""
    return {part.lower() for part in token.split('_')
            if len(part) >= 4 and part.lower() not in SEMANTIC_PATCH_STOPWORDS}


def _semantic_patch_tokens(value: str) -> set[str]:
    """Tokenize patch identifiers without treating overlap as equivalence proof."""
    result = set()
    for token in re.findall(r'[A-Za-z][A-Za-z0-9_]{3,}', value):
        token = token.lower()
        if token not in SEMANTIC_PATCH_STOPWORDS:
            result.add(token)

        result.update(_semantic_token_parts(token))

    return result


def _safe_patch_name(value: str) -> Path:
    """Reject absolute or parent-relative patch filenames from quilt series."""
    name = Path(value)
    if name.is_absolute() or '..' in name.parts:
        raise ValueError('Unsafe quilt patch path')

    return name


def _single_patch_name(line: str) -> Path | None:
    """Select active, option-free patches eligible for advisory overlap probes."""
    words = line.split()
    if len(words) != 1 or words[0].startswith('#'):
        return None

    return _safe_patch_name(words[0])


def _upstream_overlap_target(target: str) -> bool:
    """Limit overlap evidence to relative upstream paths outside Debian metadata."""
    path = Path(target)
    return not (path.is_absolute() or '..' in path.parts or path.parts[:1] == ('debian',))


def _overlap_targets(text: str) -> list[str]:
    """Preserve unique section targets, rejecting the whole patch on unsafe paths."""
    targets = []
    for target in re.findall(r'^\+\+\+\s+(?:b/)?([^\t\n ]+)', text, re.M):
        if not _upstream_overlap_target(target):
            return []

        if target not in targets:
            targets.append(target)

    return targets


def _added_patch_lines(text: str) -> str:
    """Extract added payload while excluding unified-diff file headers."""
    return '\n'.join(line[1:] for line in text.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def _unresolved_patch(tree: Path, data: bytes) -> bool:
    """Require both forward and reverse zero-fuzz probes to fail before suggesting overlap."""
    probe = ['patch', '--dry-run', '--batch', '--force', '--fuzz=0', '-p1']
    if subprocess.run(probe, input=data, cwd=tree, capture_output=True).returncode == 0:
        return False

    return subprocess.run(probe + ['--reverse'], input=data, cwd=tree,
                          capture_output=True).returncode != 0


def _current_target_texts(tree: Path, targets: list[str]) -> list[str]:
    """Read every target only when the complete target set contains regular files."""
    parts = []
    for target in targets:
        path = tree / target
        if not path.is_file() or path.is_symlink():
            return []

        parts.append(path.read_text(errors='replace'))

    return parts


def _definitions_present(definitions: set[str], parts: list[str]) -> bool:
    """Require each added definition name somewhere in the current target texts."""
    return all(any(definition in part for part in parts) for definition in definitions)


@dataclass(frozen=True)
class PatchOverlap:
    """Advisory identifiers and file scope extracted from one unresolved patch."""

    targets: list[str]
    tokens: set[str]
    definitions: set[str]

    @classmethod
    def parse(cls, text: str) -> PatchOverlap | None:
        """Require enough scoped identifier evidence to make overlap scoring useful."""
        targets = _overlap_targets(text)
        added = _added_patch_lines(text)
        tokens = _semantic_patch_tokens(added)
        definitions = set(re.findall(r'\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)', added))
        if not targets or len(tokens) < 8 or not definitions:
            return None

        return cls(targets, tokens, definitions)

    def current_overlap(self, tree: Path) -> float | None:
        """Measure current-source overlap only after target and definition checks."""
        parts = _current_target_texts(tree, self.targets)
        if not parts or not _definitions_present(self.definitions, parts):
            return None

        return self.overlap('\n'.join(parts))

    def overlap(self, text: str) -> float:
        """Calculate the fraction of patch identifiers present in candidate evidence."""
        return len(self.tokens & _semantic_patch_tokens(text)) / len(self.tokens)

    def commit_overlap(self, checkout: Path, commit: dict) -> float | None:
        """Score a commit's scoped diff and message, ignoring failed or empty lookups."""
        result = subprocess.run(
            ['git', 'show', '--format=%B', commit['sha'], '--', *self.targets],
            cwd=checkout, text=True, capture_output=True, timeout=30)
        if result.returncode or not result.stdout.strip():
            return None

        return self.overlap(result.stdout)

    def best_commit(self, checkout: Path, commits: list[dict]) -> dict | None:
        """Retain the strongest qualifying commit using the existing rounded-score comparison."""
        best = None
        for commit in commits:
            overlap = self.commit_overlap(checkout, commit)
            if _better_overlap(overlap, best):
                best = {'sha': commit['sha'], 'subject': commit.get('subject', ''),
                        'commit_overlap': round(overlap, 3)}

        return best

    def suggestion(self, tree: Path, checkout: Path, commits: list[dict], patch: Path, name: Path) -> dict | None:
        """Return a human-review suggestion without editing patch content or the series."""
        overlap = self.current_overlap(tree)
        if overlap is None or overlap < 0.70:
            return None

        best = self.best_commit(checkout, commits)
        if best is None:
            return None

        return {
            'action': 'review-upstream-patch-overlap', 'name': str(name),
            'sha256': sha256(patch), 'upstream_commit': best,
            'current_overlap': round(overlap, 3), 'token_count': len(self.tokens),
            'targets': self.targets, 'human_review_required': True,
        }


def _better_overlap(overlap: float | None, best: dict | None) -> bool:
    """Apply the threshold before comparing a score with the previous best result."""
    if overlap is None or overlap < 0.70:
        return False

    return best is None or overlap > best['commit_overlap']


def _patch_overlap_suggestion(tree: Path, checkout: Path, commits: list[dict], name: Path) -> dict | None:
    """Probe a regular patch before attempting advisory semantic comparison."""
    patch = tree / 'debian/patches' / name
    if not patch.is_file() or patch.is_symlink():
        return None

    data = patch.read_bytes()
    if not _unresolved_patch(tree, data):
        return None

    overlap = PatchOverlap.parse(data.decode(errors='replace'))
    if overlap is None:
        return None

    return overlap.suggestion(tree, checkout, commits, patch, name)


def _overlap_suggestions(lines: list[str], tree: Path, checkout: Path, commits: list[dict]) -> list[dict]:
    """Collect suggestions for eligible active patch entries in series order."""
    adjustments = []
    for line in lines:
        name = _single_patch_name(line)
        if name is None:
            continue

        suggestion = _patch_overlap_suggestion(tree, checkout, commits, name)
        if suggestion is not None:
            adjustments.append(suggestion)

    return adjustments


def superseded_upstream_patches(tree: Path, checkout: Path, selected: dict) -> list[dict]:
    """Suggest upstream commits for review without disabling a quilt patch.

    Identifier overlap is a search heuristic, never proof of equivalent
    behavior. Keep the patch active so source validation still fails closed.
    """
    series = tree / 'debian/patches/series'
    if not series.is_file():
        return []

    lines = series.read_text().splitlines()
    commits = [item for item in selected.get('commits', [])
               if re.fullmatch(r'[0-9a-f]{40}', item.get('sha', ''))]
    if not commits:
        return []

    return _overlap_suggestions(lines, tree, checkout, commits)
