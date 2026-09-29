import hashlib
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def test_cache_key_binds_published_image_and_preparation_script(tmp_path):
    digest = 'a' * 64
    tools = tmp_path / 'bin'
    tools.mkdir()
    curl = tools / 'curl'
    curl.write_text(f'#!/bin/sh\nprintf "%s *resolute-server-cloudimg-amd64.img\\n" "{digest}"\n')
    curl.chmod(0o755)
    output = tmp_path / 'github-output'

    completed = subprocess.run(
        ['bash', 'scripts/autopkgtest-cache-key.sh', 'resolute', 'amd64'],
        cwd=ROOT, text=True, capture_output=True, check=True,
        env={**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}',
             'GITHUB_OUTPUT': str(output)},
    )

    script_digest = hashlib.sha256((ROOT / 'scripts/prepare-autopkgtest.sh').read_bytes()).hexdigest()
    expected = f'autopkgtest-v1-resolute-amd64-{digest}-{script_digest}'
    assert completed.stdout.strip() == expected
    assert f'key={expected}' in output.read_text()
    assert f'upstream_sha256={digest}' in output.read_text()


def test_cache_key_rejects_unsupported_target():
    completed = subprocess.run(
        ['bash', 'scripts/autopkgtest-cache-key.sh', 'unsupported', 'amd64'],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert completed.returncode == 2
    assert 'Unsupported suite' in completed.stderr
