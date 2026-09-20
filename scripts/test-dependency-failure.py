"""Deliberately corrupt a producer checksum and assert its consumer is blocked."""
import json
import subprocess
import sys
from pathlib import Path

lock_path = Path(sys.argv[1])
root = Path(sys.argv[2] if len(sys.argv) > 2 else 'artifacts/dependency-negative').resolve()
root.mkdir(parents=True, exist_ok=True)
lock = json.loads(lock_path.read_text())
producer, consumer = lock['packages']
assert producer['source'] in consumer['depends_on']
producer['input']['tarball']['sha256'] = '0' * 64
invalid = root / 'intentional-checksum-failure.json'
invalid.write_text(json.dumps(lock, indent=2) + '\n')
result = subprocess.run([sys.executable, '-m', 'packagetest.cli', 'build', '--plan', str(invalid), '--run-dir', str(root)], capture_output=True, text=True)
(root / 'stdout.log').write_text(result.stdout)
(root / 'stderr.log').write_text(result.stderr)
if result.returncode != 1:
    raise SystemExit(f'Expected build failure, got {result.returncode}')
summary = json.loads(result.stdout)
manifest = json.loads((Path(summary['run_dir']) / 'generation-manifest.json').read_text())
first, second = manifest['packages']
assert first['result'] == 'FAILED' and 'Checksum mismatch' in first['error'], first
assert second['result'] == 'BLOCKED' and second['blocked_by'] == [producer['source']], second
assert not (Path(summary['run_dir']) / 'work' / consumer['source']).exists()
report = {'test_result': 'PASSED', 'expected_build_result': 'FAILED', 'run_dir': summary['run_dir'],
          'producer_result': first['result'], 'consumer_result': second['result'], 'blocked_by': second['blocked_by']}
(root / 'assertions.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
