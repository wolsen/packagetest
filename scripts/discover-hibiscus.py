#!/usr/bin/env python3
"""Refresh the audited release inventory from an existing releases checkout."""
import argparse
from pathlib import Path
from packagetest.catalog import make_catalog, write_catalog

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--releases', required=True, type=Path)
parser.add_argument('--source-index', required=True, action='append', type=Path)
parser.add_argument('--output', type=Path, default=Path('config/hibiscus-catalog.json'))
args = parser.parse_args()
catalog = make_catalog(args.releases, args.source_index)
write_catalog(catalog, args.output)
print(f'{len(catalog["packages"])} packages; {len(catalog["exclusions"])} explicit exclusions; {args.output}')
