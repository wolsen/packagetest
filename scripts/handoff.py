#!/usr/bin/env python3
"""Prepare or consume same-run GitHub artifact handoffs."""
import argparse
import json
from pathlib import Path
from packagetest.handoff import build_repository, collect_producers, stamp_generation

parser = argparse.ArgumentParser(description=__doc__)
sub = parser.add_subparsers(dest='command', required=True)
stamp = sub.add_parser('stamp')
stamp.add_argument('manifest', type=Path)
stamp.add_argument('--run-id', required=True)
stamp.add_argument('--run-attempt', required=True)
collect = sub.add_parser('collect')
collect.add_argument('--downloads', required=True, type=Path)
collect.add_argument('--locks', required=True, type=Path, help='JSON source-to-lock mapping')
collect.add_argument('--run-id', required=True)
collect.add_argument('--run-attempt', required=True)
collect.add_argument('--output', required=True, type=Path)
repo = sub.add_parser('repository')
repo.add_argument('--inputs', required=True, type=Path)
repo.add_argument('--output', required=True, type=Path)
args = parser.parse_args()
if args.command == 'stamp':
    result = stamp_generation(args.manifest, run_id=args.run_id, run_attempt=args.run_attempt)
elif args.command == 'collect':
    locks = json.loads(args.locks.read_text())
    if not locks:
        parser.error('Expected at least one producer lock')
    target = next(iter(locks.values()))['target']
    if any(lock['target'] != target for lock in locks.values()):
        parser.error('Producer locks must share one target')
    result = collect_producers(args.downloads, locks, run_id=args.run_id, run_attempt=args.run_attempt, target=target)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
else:
    result = build_repository(json.loads(args.inputs.read_text()), args.output)
print(json.dumps(result, indent=2))
