#!/usr/bin/env python3
"""Repair packaging drift that prevents creation of an immutable source package."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from packagetest.failure_analysis import (normalize_repair_decision,
                                          parse_repair_decision,
                                          validate_repair_decision,
                                          validate_source_patch)


def remediation_module():
    path = Path(__file__).with_name('remediate-package.py')
    spec = importlib.util.spec_from_file_location('packagetest_package_remediation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def failed_tree(work: Path, source: str) -> Path:
    matches = [path.parent.parent for path in work.glob(f'{source}-*/debian/control')]
    if len(matches) != 1:
        raise ValueError(f'expected one failed prepared source tree, found {len(matches)}')
    return matches[0]


def failure_evidence(work: Path, output: Path, limit: int = 20_000) -> str:
    blocks = []
    resolution = work / 'resolution.json'
    result = output / 'result.json'
    for path in (resolution, result):
        if path.is_file():
            blocks.append(path.read_text(errors='replace'))
    logs = work / 'logs'
    if logs.is_dir():
        for path in sorted(logs.glob('*.log')):
            text = path.read_text(errors='replace')
            if any(marker in text for marker in
                   ('Hunk #', '.patch', 'dpkg-source: error', 'does not apply')):
                blocks.append(f'--- {path.name} ---\n{text}')
    value = '\n\n'.join(blocks)
    return value[-limit:]


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--initial-work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--llama-cli', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--model-sha256', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--model-timeout', type=int, default=180)
    parser.add_argument('--max-model-calls', type=int, default=2)
    args = parser.parse_args()
    helper = remediation_module()
    args.report.mkdir(parents=True, exist_ok=True)
    if helper.sha256(args.model) != args.model_sha256:
        raise SystemExit('local model checksum mismatch')
    tree = failed_tree(args.initial_work, args.source)
    evidence = failure_evidence(args.initial_work, args.output)
    (args.report / 'failure-evidence.txt').write_text(evidence + '\n')
    decisions, attempts, selected = [], [], None
    feedback = ''
    for number in range(1, args.max_model_calls + 1):
        attempt = args.report / f'attempt-{number}'
        attempt.mkdir()
        prompt = helper.prompt(args.source, 'source-preparation', evidence,
                               helper.source_context(tree, evidence), feedback)
        (attempt / 'prompt.txt').write_text(prompt)
        record = {'number': number, 'result': 'MODEL_ERROR'}
        try:
            output, inference = helper.llama_generate(
                args.llama_cli, args.model, prompt, number, args.model_timeout)
            (attempt / 'model-output.txt').write_text(output)
            decision = normalize_repair_decision(parse_repair_decision(output), tree, evidence)
            write_json(attempt / 'decision.json', decision)
            validation = validate_repair_decision(decision, evidence)
            write_json(attempt / 'decision-validation.json', validation)
            if validation['result'] != 'ACCEPTED':
                raise ValueError(validation['error'])
            if decision['action'] == 'no_fix':
                record.update(result='NO_FIX', decision=decision, inference=inference)
                attempts.append(record)
                break
            if decision['action'] not in {'refresh_quilt_patch', 'drop_quilt_patch'}:
                raise ValueError('source preparation currently accepts only quilt refresh or drop decisions')
            decisions.append(decision)
            patch = helper.render_cumulative_repair(decisions, tree)
            patch_path = attempt / 'proposal.patch'
            patch_path.write_text(patch)
            patch_validation = validate_source_patch(patch, tree)
            write_json(attempt / 'patch-validation.json', patch_validation)
            if patch_validation['result'] != 'APPLIES':
                raise ValueError(patch_validation['error'])
            candidate_work = args.work / f'attempt-{number}-work'
            candidate_output = args.work / f'attempt-{number}-source'
            log = attempt / 'source-preparation.log'
            command = [sys.executable, 'scripts/prepare-nightly-source.py',
                       '--catalog', str(args.catalog), '--source', args.source,
                       '--work', str(candidate_work), '--output', str(candidate_output),
                       '--run-id', args.run_id, '--run-attempt', args.run_attempt,
                       '--remediation-patch', str(patch_path)]
            with log.open('w') as stream:
                completed = subprocess.run(
                    command, stdout=stream, stderr=subprocess.STDOUT, text=True,
                    env={**os.environ, 'PYTHONPATH': 'src'}, timeout=3600)
            record.update(result='REPAIRED' if completed.returncode == 0 else 'PREPARATION_FAILED',
                          decision=decision, inference=inference,
                          patch_validation=patch_validation,
                          preparation_returncode=completed.returncode)
            attempts.append(record)
            if completed.returncode == 0 and (candidate_output / 'prepared-source.json').is_file():
                selected = number
                shutil.rmtree(args.output)
                shutil.move(str(candidate_output), args.output)
                break
            evidence = failure_evidence(candidate_work, candidate_output) + '\n' + log.read_text(errors='replace')[-8000:]
            feedback = json.dumps(record, indent=2)
        except Exception as exc:
            record['error'] = str(exc)
            attempts.append(record)
            feedback = json.dumps(record, indent=2)
    report = {
        'schema_version': 1, 'source': args.source,
        'result': 'REPAIRED' if selected else 'UNRESOLVED',
        'selected_attempt': selected, 'attempts': attempts,
        'model': {'name': helper.MODEL_NAME, 'sha256': args.model_sha256},
        'ci': {'run_id': args.run_id, 'run_attempt': args.run_attempt,
               'checkout_sha': os.environ.get('GITHUB_SHA')},
        'finished_at': datetime.now(timezone.utc).isoformat(),
    }
    write_json(args.report / 'result.json', report)
    summary = [f"## {args.source} source-preparation remediation: {report['result']}", '',
               '| Attempt | Decision | Validation | Preparation | Result |',
               '|---:|---|---|---|---|']
    for item in attempts:
        summary.append(f"| {item['number']} | {item.get('decision', {}).get('action', '—')} | "
                       f"{item.get('patch_validation', {}).get('result', '—')} | "
                       f"{item.get('preparation_returncode', '—')} | {item['result']} |")
    summary.extend(['', 'Any packaging change is a proposal requiring human review.'])
    (args.report / 'summary.md').write_text('\n'.join(summary) + '\n')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write((args.report / 'summary.md').read_text())
    destination = args.output / 'ai-source-remediation'
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(args.report, destination)
    return 0 if selected else 1


if __name__ == '__main__':
    raise SystemExit(main())
