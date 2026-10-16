#!/usr/bin/env python3
"""Publish compact package reports, accepted producers, and failure-only diagnostics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from collections.abc import Callable
from typing import Any


def read_record(path: Path, source: str) -> dict[str, Any]:
    """Read one local verdict without accepting evidence for another source."""
    record = json.loads(path.read_text())
    if not isinstance(record, dict):
        raise ValueError(f'Expected a JSON object in {path}')

    if record.get('source', source) != source:
        raise ValueError(f'Artifact source mismatch in {path}')

    return record


def diagnostic_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Omit disposable workspaces, recovered baselines, and VM disks from diagnostics."""
    path = PurePosixPath(member.name)
    if 'work' in path.parts or path.suffix == '.qcow2':
        return None

    baseline = PurePosixPath('outputs/source-preparation/remediation-source')
    if path == baseline or baseline in path.parents:
        return None

    return member


def write_archive(destination: Path, members: list[tuple[Path, str]],
                  filter_member: Callable[[tarfile.TarInfo], tarfile.TarInfo | None] | None = None) -> None:
    """Create one archive with stable relative roots and no dereferencing of links."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, 'w:gz') as archive:
        for path, name in members:
            archive.add(path, arcname=name, filter=filter_member)


def copy_optional(source: Path, destination: Path) -> None:
    """Copy a compact evidence file when that stage produced it."""
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


class PackageArtifacts:
    """Own publication decisions and the three output directories for one package."""

    def __init__(self, source: str, outputs: Path, tests: Path, remediation: Path,
                 destination: Path) -> None:
        """Capture final verdicts and readiness before deciding which payloads to retain."""
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', source):
            raise ValueError('Invalid Debian source name')

        self.source = source
        self.outputs = outputs
        self.tests = tests
        self.remediation = remediation
        self.destination = destination
        self.build_path = outputs / 'result.json'
        self.test_path = tests / 'result/result.json'
        self.build = read_record(self.build_path, source)
        self.test = read_record(self.test_path, source)
        self.readiness_path = self._readiness_path()
        self.readiness = self._readiness()

    def _readiness_path(self) -> Path | None:
        """Require at most one canonical readiness record, including infrastructure failures."""
        paths = list(self.outputs.glob('gen-*/producer-readiness.json'))
        root = self.outputs / 'producer-readiness.json'
        if root.is_file():
            paths.append(root)

        if len(paths) > 1:
            raise ValueError('Ambiguous producer readiness records')

        return paths[0] if paths else None

    def _readiness(self) -> dict[str, Any]:
        """Keep missing readiness nonpublishable while retaining its final report."""
        if self.readiness_path is None:
            return {}

        return read_record(self.readiness_path, self.source)

    @property
    def ready(self) -> bool:
        """Consume readiness only when it agrees with the final build and test records."""
        return (self.readiness.get('result') == 'READY'
                and self.build.get('result') == 'SUCCEEDED'
                and self.readiness.get('autopkgtest_result') == self.test.get('result'))

    def _report(self) -> None:
        """Retain complete structured verdicts, evolution, proposals, and accepted repairs."""
        report = self.destination / 'report'
        report.mkdir()
        shutil.copy2(self.build_path, report / 'build.json')
        shutil.copy2(self.test_path, report / 'autopkgtest.json')
        if self.readiness_path is not None:
            shutil.copy2(self.readiness_path, report / 'producer-readiness.json')

        proposal = self.outputs / 'source-preparation/packaging-proposal'
        if proposal.is_dir():
            shutil.copytree(proposal, report / 'packaging-proposal')

        self._repair_summary(report / 'remediation')

    def _repair_summary(self, destination: Path) -> None:
        """Keep repair outcomes and the accepted cumulative patch without copying model logs."""
        for name in ('result.json', 'summary.md'):
            copy_optional(self.remediation / name, destination / name)

        result = self.remediation / 'result.json'
        if result.is_file():
            selected = read_record(result, self.source).get('selected_attempt')
            if selected is not None:
                copy_optional(self.remediation / f'attempt-{int(selected)}/proposal.patch',
                              destination / 'accepted-repair.patch')

    def _producer(self) -> None:
        """Publish only the generation identity and verified source/binary handoff files."""
        if self.readiness_path is None:
            raise ValueError('Ready producer has no readiness record')

        generation = self.readiness_path.parent
        manifest = generation / 'generation-manifest.json'
        if not manifest.is_file():
            raise ValueError('Ready producer has no generation manifest')

        names = ('generation-manifest.json', 'build-lock.json', 'producer-readiness.json')
        members = [(generation / name, f'{generation.name}/{name}') for name in names]
        artifacts = generation / 'artifacts' / self.source
        members.append((artifacts, f'{generation.name}/artifacts/{self.source}'))
        write_archive(self.destination / 'producer' / f'{self.source}.tar.gz', members)

    def _diagnostics(self) -> None:
        """Retain full failed build, test, and AI evidence together in one diagnostic archive."""
        members = [(self.outputs, 'outputs'), (self.tests, 'test-results')]
        if self.remediation.is_dir():
            members.append((self.remediation, 'ai-remediation'))

        write_archive(self.destination / 'diagnostics' / f'{self.source}.tar.gz',
                      members, diagnostic_member)

    def publish(self) -> dict[str, bool]:
        """Always retain a report; blocked consumers need no duplicate diagnostic payload."""
        self.destination.mkdir(parents=True, exist_ok=False)
        self._report()
        diagnostics = not self.ready and self.build.get('result') != 'BLOCKED'
        if self.ready:
            self._producer()
        elif diagnostics:
            self._diagnostics()

        return {'producer': self.ready, 'diagnostics': diagnostics}


def main() -> None:
    """Stage package artifacts and expose publication flags to the composite workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--outputs', required=True, type=Path)
    parser.add_argument('--tests', required=True, type=Path)
    parser.add_argument('--remediation', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    flags = PackageArtifacts(args.source, args.outputs, args.tests, args.remediation, args.output).publish()
    if args.github_output is not None:
        with args.github_output.open('a') as handle:
            for name, value in flags.items():
                handle.write(f'{name}={str(value).lower()}\n')

    print(json.dumps(flags, sort_keys=True))


if __name__ == '__main__':
    main()
