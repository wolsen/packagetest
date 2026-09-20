"""Run installed-package tests in a fresh VM, preserving exact candidate provenance."""
from __future__ import annotations

import json
import os
import signal
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .artifacts import sha256, verify_source, verify_binaries


def classify(returncode: int, summary: str) -> dict:
    tests = []
    for line in summary.splitlines():
        match = re.match(r'^(\S+)\s+(PASS|FAIL|SKIP|FLAKY)(?:\s+(.*))?$', line)
        if match:
            tests.append(dict(zip(('name', 'result', 'detail'), (match[1], match[2], match[3] or ''))))
    if returncode in {16, 20} or returncode < 0:
        result = 'INFRA_ERROR'
    elif returncode in {4, 6, 12, 14} or any(t['result'] == 'FAIL' for t in tests):
        result = 'FAIL'
    elif returncode == 8:
        result = 'SKIP' if any(t['name'] != '*' for t in tests) else 'NO_TESTS'
    elif returncode == 2 or any(t['result'] in {'SKIP', 'FLAKY'} for t in tests):
        result = 'SKIP'
    elif returncode == 0 and any(t['result'] == 'PASS' for t in tests):
        result = 'PASS'
    else:
        result = 'INFRA_ERROR'
    return {'result': result, 'returncode': returncode, 'tests': tests}


def checked_file(directory: Path, record: dict) -> Path:
    name = record['file']
    if Path(name).name != name:
        raise ValueError('Artifact filename is not a basename')
    path = directory / name
    if not path.is_file() or sha256(path) != record['sha256']:
        raise ValueError(f'Missing or corrupt artifact: {path}')
    return path


def manifest_inputs(path: Path, source: str) -> tuple[dict, dict, Path, list[Path]]:
    manifest = json.loads(path.read_text())
    package = next(p for p in manifest['packages'] if p['source'] == source)
    if package['result'] != 'SUCCEEDED':
        raise ValueError(f'Cannot test unsuccessful build: {source}')
    base = path.parent / 'artifacts' / source
    sources = [checked_file(base / 'source', record) for record in package['source_artifacts']]
    dscs = [p for p in sources if p.suffix == '.dsc']
    if len(dscs) != 1:
        raise ValueError('Expected one source descriptor')
    verify_source(dscs[0], source, package['version'])
    binaries = [checked_file(base / 'binary', record) for record in package['binaries']]
    verify_binaries(base / 'binary', source=source, version=package['version'],
                    expected=[b['package'] for b in package['binaries']], arch=manifest['target']['architecture'])
    return manifest, package, dscs[0], binaries


def run(manifest_path: Path, source: str, output: Path, *, backend: str, image: str,
        dependency_repository: Path | None = None, timeout: int = 7200) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'source': source, 'result': 'INFRA_ERROR', 'backend': backend, 'image': image}
    try:
        manifest, package, dsc, binaries = manifest_inputs(manifest_path.resolve(), source)
        report.update(generation_id=manifest['generation_id'], version=package['version'],
                      lock_sha256=manifest['lock_sha256'], target=manifest['target'])
        if backend not in {'qemu', 'lxd'}:
            raise ValueError('Only isolated qemu and lxd testbeds are supported')
        candidates = {p.name: p for p in binaries}
        if dependency_repository:
            provenance = json.loads((dependency_repository / 'provenance.json').read_text())
            for record in provenance['packages']:
                relative = Path(record['path'])
                path = (dependency_repository / relative).resolve()
                if not path.is_relative_to(dependency_repository.resolve()) or not path.is_file() or sha256(path) != record['sha256']:
                    raise ValueError(f'Corrupt dependency repository artifact: {relative}')
                if path.name in candidates and sha256(candidates[path.name]) != record['sha256']:
                    raise ValueError(f'Conflicting candidate: {path.name}')
                candidates[path.name] = path
            report['dependency_provenance'] = provenance
        repo = output / 'candidate-repository'
        repo.mkdir()
        for name, path in candidates.items():
            shutil.copy2(path, repo / name)
        with (repo / 'Packages').open('w') as handle:
            subprocess.run(['dpkg-scanpackages', '.', '/dev/null'], cwd=repo, stdout=handle, check=True)
        expected = {b['package']: b['version'] for b in package['binaries']}
        pin = []
        versions = {}
        for path in candidates.values():
            name, version = subprocess.check_output(['dpkg-deb', '-f', str(path), 'Package', 'Version'], text=True).strip().splitlines()
            # dpkg-deb emits field labels when multiple fields are requested.
            name, version = name.removeprefix('Package: '), version.removeprefix('Version: ')
            if name in versions and versions[name] != version:
                raise ValueError(f'Multiple versions in candidate repository: {name}')
            versions[name] = version
            pin.append(f'Package: {name}\nPin: version {version}\nPin-Priority: 1001\n')
        (repo / 'preferences').write_text('\n'.join(pin))
        install = ' '.join(shlex.quote(f'{name}={version}') for name, version in expected.items())
        checks = '\n'.join(f'test "$(dpkg-query -W -f=\'${{Version}}\' {shlex.quote(name)})" = {shlex.quote(version)}' for name, version in expected.items())
        setup = output / 'setup.sh'
        setup.write_text('set -eu\n'
                         'test "$(. /etc/os-release; echo "$VERSION_CODENAME")" = ' + shlex.quote(manifest['target']['suite']) + '\n'
                         "printf '%s\\n' 'deb [trusted=yes] file:/opt/packagetest-candidate ./' > /etc/apt/sources.list.d/packagetest.list\n"
                         'cp /opt/packagetest-candidate/preferences /etc/apt/preferences.d/packagetest\n'
                         'apt-get update\n'
                         f'DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades {install}\n{checks}\n'
                         'dpkg-query -W | tee /opt/packagetest-candidate/installed-versions.txt\n')
        command = ['autopkgtest', '--no-built-binaries', '--no-apt-fallback',
                   '--output-dir=' + str(output / 'testbed'), '--summary=' + str(output / 'summary'),
                   '--timeout-test=' + str(timeout), '--timeout-install=1800',
                   '--copy=' + str(repo.resolve()) + ':/opt/packagetest-candidate',
                   '--setup-commands=' + str(setup.resolve()), str(dsc),
                   *[str(p) for p in candidates.values()], '--', backend]
        if backend == 'qemu':
            command += ['--ram-size=4096', '--cpus=2', str(Path(image).resolve())]
        else:
            command += [image]
        report.update(command=command, expected_versions=expected, candidate_versions=versions)
        with (output / 'console.log').open('w') as log:
            with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True) as process:
                try:
                    returncode = process.wait(timeout=timeout + 3600)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
        summary_path = output / 'summary'
        report.update(classify(returncode, summary_path.read_text() if summary_path.exists() else ''))
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        report.update(result='INFRA_ERROR', error=str(error))
    finally:
        (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report
