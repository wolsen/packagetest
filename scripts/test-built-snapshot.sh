#!/usr/bin/env bash
# Run installed autopkgtests in the same GitHub job as the package build.
# Always preserve status and evidence. Coverage gaps are nonfatal; package,
# infrastructure, and blocked results fail the job.
set -uo pipefail

source_name=${1:?source package is required}
repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
suite=$(python3 -c 'import json; print(json.load(open("nightly-plan/catalog.json"))["target"]["suite"])')
result_root=test-results/result
mkdir -p test-results
test_rc=0

build_ready=$(SOURCE="$source_name" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('outputs/result.json')
ready = path.exists() and json.loads(path.read_text()).get('result') == 'SUCCEEDED'
print(str(ready).lower())
if not ready:
    output = Path('test-results/result')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_text(json.dumps({
        'schema_version': 1,
        'source': os.environ['SOURCE'],
        'result': 'BLOCKED',
        'error': 'No successful source build available in this package job',
        'ci': {
            'run_id': os.environ['GITHUB_RUN_ID'],
            'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
        },
    }, indent=2) + '\n')
PY
)

if [[ "$build_ready" == true ]]; then
  set +e
  test_image=$(bash scripts/prepare-autopkgtest.sh "$suite" "$RUNNER_TEMP/autopkgtest-image")
  prepare_rc=$?
  set -e
  if [[ $prepare_rc -eq 0 ]]; then
    set +e
    env PYTHONPATH=src python3 scripts/nightly-autopkgtest.py \
      --catalog nightly-plan/catalog.json --source "$source_name" \
      --inputs inputs --candidate-input outputs \
      --output "$result_root" --run-id "$GITHUB_RUN_ID" \
      --run-attempt "$GITHUB_RUN_ATTEMPT" --image "$test_image" \
      2>&1 | tee test-results/autopkgtest.log
    test_rc=${PIPESTATUS[0]}
    set -e
  else
    test_rc=1
  fi
else
  test_rc=1
fi

if [[ -f "$RUNNER_TEMP/autopkgtest-image/prepare.log" ]]; then
  cp "$RUNNER_TEMP/autopkgtest-image/prepare.log" test-results/prepare.log
fi

PYTHONPATH="$repository_root/src" SOURCE="$source_name" python3 - <<'PY'
import json
import os
from pathlib import Path

from packagetest.autopkgtest import diagnostic_message

path = Path('test-results/result/result.json')
if not path.exists():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'schema_version': 1,
        'source': os.environ['SOURCE'],
        'result': 'INFRA_ERROR',
        'error': 'Autopkgtest setup did not complete',
        'ci': {
            'run_id': os.environ['GITHUB_RUN_ID'],
            'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
        },
    }, indent=2) + '\n')

report = json.loads(path.read_text())
result = report['result']
raw = report.get('returncode', 'not run')
tests = report.get('tests', [])
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write(f"## {os.environ['SOURCE']} autopkgtest: {result}\n\n")
    summary.write(f"Raw autopkgtest return code: `{raw}`.\n\n")
    if result in {'SUPERFICIAL', 'SKIP', 'NO_TESTS'}:
        summary.write('This coverage gap is recorded but does not fail the package job.\n\n')
    if tests:
        summary.write('| Test | Result | Detail |\n|---|---|---|\n')
        for test in tests:
            detail = test.get('detail', '').replace('|', '\\|')
            summary.write(f"| {test['name']} | {test['result']} | {detail} |\n")
    if report.get('error'):
        summary.write(f"\nDiagnostic: `{report['error']}`\n")
command = ('error' if result in {'FAIL', 'INFRA_ERROR', 'BLOCKED'} else 'notice')
detail = diagnostic_message(report)
message = f'{os.environ["SOURCE"]}: raw return code {raw}; {detail}'
message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
print(f'::{command} title=Autopkgtest {result}::{message}')
PY

mkdir -p test-bundles
sudo tar -czf "test-bundles/$source_name.tar.gz" -C test-results .
sudo chmod a+r "test-bundles/$source_name.tar.gz"

# The raw autopkgtest code remains in result.json.  Coverage gaps are visible
# but nonfatal; only classified correctness/infrastructure failures fail here.
result=$(python3 -c 'import json; print(json.load(open("test-results/result/result.json"))["result"])')
case "$result" in
  PASS|SUPERFICIAL|SKIP|NO_TESTS) exit 0 ;;
  *) exit 1 ;;
esac
