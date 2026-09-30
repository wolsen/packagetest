import hashlib
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def test_cache_key_binds_daily_update_policy_and_builder_script(tmp_path):
    output = tmp_path / 'github-output'
    completed = subprocess.run(
        ['bash', 'scripts/sbuild-cache-key.sh', 'resolute', 'amd64'],
        cwd=ROOT, text=True, capture_output=True, check=True,
        env={**os.environ, 'SBUILD_CACHE_DATE': '2026-09-29',
             'GITHUB_OUTPUT': str(output)},
    )

    script_digest = hashlib.sha256((ROOT / 'scripts/prepare-builder.sh').read_bytes()).hexdigest()
    expected = f'sbuild-rootfs-v1-ubuntu-24.04-resolute-amd64-2026-09-29-{script_digest}'
    assert completed.stdout.strip() == expected
    assert output.read_text() == f'key={expected}\n'


def test_cache_key_rejects_unsupported_target():
    completed = subprocess.run(
        ['bash', 'scripts/sbuild-cache-key.sh', 'unsupported', 'amd64'],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert completed.returncode == 2
    assert 'Unsupported suite' in completed.stderr
