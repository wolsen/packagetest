#!/usr/bin/env python3
"""Generate and preflight the OpenStack/Ubuntu catalog for one pipeline run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from packagetest.catalog import (download_source_indexes, make_catalog,
                                 ubuntu_source_indexes, validate_archive_sources,
                                 write_catalog)


def clone_releases(destination: Path, repository: str, ref: str) -> None:
    subprocess.run([
        'git', 'clone', '--quiet', '--depth', '1', '--branch', ref,
        repository, str(destination),
    ], check=True)


def generate(*, output: Path, workspace: Path, suite: str, series: str,
             codename: str, releases_repository: str, releases_ref: str,
             validate_sources: bool = True) -> dict:
    releases = workspace / 'releases'
    indexes = workspace / 'indexes'
    workspace.mkdir(parents=True, exist_ok=False)
    clone_releases(releases, releases_repository, releases_ref)
    index_paths = download_source_indexes(suite, indexes)
    catalog = make_catalog(releases, index_paths, series=series,
                           codename=codename, suite=suite)
    index_urls = {item['filename']: item['url'] for item in ubuntu_source_indexes(suite)}
    for key in ('archive_indexes', 'archive_python_indexes'):
        for item in catalog[key]:
            item['url'] = index_urls[item['filename']]
    if validate_sources:
        validate_archive_sources(catalog)
    write_catalog(catalog, output)
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=Path('nightly-plan/catalog-input.json'))
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--suite', default='resolute')
    parser.add_argument('--series', default='2026.2')
    parser.add_argument('--codename', default='hibiscus')
    parser.add_argument('--releases-repository',
                        default='https://opendev.org/openstack/releases')
    parser.add_argument('--releases-ref', default='master')
    parser.add_argument('--skip-source-validation', action='store_true',
                        help='Development only: do not verify selected archive files')
    args = parser.parse_args()

    if args.workspace:
        catalog = generate(
            output=args.output, workspace=args.workspace, suite=args.suite,
            series=args.series, codename=args.codename,
            releases_repository=args.releases_repository,
            releases_ref=args.releases_ref,
            validate_sources=not args.skip_source_validation)
    else:
        with tempfile.TemporaryDirectory(prefix='packagetest-catalog-') as directory:
            catalog = generate(
                output=args.output, workspace=Path(directory) / 'work', suite=args.suite,
                series=args.series, codename=args.codename,
                releases_repository=args.releases_repository,
                releases_ref=args.releases_ref,
                validate_sources=not args.skip_source_validation)
    print(json.dumps({
        'packages': len(catalog['packages']),
        'exclusions': len(catalog['exclusions']),
        'release_metadata': catalog['release_metadata'],
        'archive_indexes': catalog['archive_indexes'],
        'output': str(args.output),
    }))


if __name__ == '__main__':
    main()
