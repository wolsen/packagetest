"""Install validated output into a fresh file-based schroot and test Oslo i18n."""
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
manifests = sorted(root.glob('gen-*/generation-manifest.json'), key=lambda p: p.stat().st_mtime)
if not manifests:
    raise SystemExit('No build manifest')
path = manifests[-1]
manifest = json.loads(path.read_text())
if manifest['result'] != 'SUCCEEDED':
    raise SystemExit('Build did not succeed')
package = manifest['packages'][0]
if package['source'] != 'python-oslo.i18n':
    raise SystemExit('This smoke test is specific to python-oslo.i18n')
chroot = manifest['target']['chroot']
# Invoke sg externally because this workflow process may predate group setup.
def run(*args, **kwargs):
    return subprocess.check_output(list(args), text=True, stderr=subprocess.STDOUT, **kwargs)
# Root session is limited to this disposable runner's fresh chroot.
session = run('sudo', 'schroot', '-b', '-c', chroot).strip()
report = {'session': session, 'result': 'FAILED'}
try:
    location = run('sudo', 'schroot', '--location', '-c', 'session:' + session).strip()
    target = Path(location) / 'tmp' / 'packagetest-debs'
    subprocess.run(['sudo', 'mkdir', '-p', str(target)], check=True)
    for binary in package['binaries']:
        source = path.parent / 'artifacts' / package['source'] / 'binary' / binary['file']
        subprocess.run(['sudo', 'cp', str(source), str(target)], check=True)
    def inside(*args):
        return run('sudo', 'schroot', '-r', '-c', session, '-u', 'root', '--directory', '/', '--', *args)
    report['apt_update'] = inside('apt-get', 'update')
    report['install'] = inside('bash', '-c', 'DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/packagetest-debs/*.deb')
    report['version'] = inside('dpkg-query', '-W', '-f=${Version}', 'python3-oslo.i18n').strip()
    if report['version'] != package['version']:
        raise RuntimeError('Installed version differs from built version')
    report['smoke'] = inside('python3', '-c', "from oslo_i18n import TranslatorFactory; t=TranslatorFactory('packagetest'); assert t.primary('hello') == 'hello'; print('oslo_i18n translation OK')")
    if package.get('snapshot'):
        report['python_distribution_version'] = inside('python3', '-c',
            "from importlib.metadata import version; print(version('oslo.i18n'))").strip()
        if report['python_distribution_version'] != package['snapshot']['pep440_version']:
            raise RuntimeError('Installed Python snapshot version differs from pinned source version')
    report['result'] = 'SUCCEEDED'
except Exception as exc:
    report['error'] = getattr(exc, 'output', str(exc))
    raise
finally:
    cleanup = subprocess.run(['sudo', 'schroot', '-e', '-c', session], capture_output=True, text=True)
    if cleanup.returncode:
        report['result'] = 'FAILED'
        report['cleanup_error'] = cleanup.stderr
    (path.parent / 'smoke-install.json').write_text(json.dumps(report, indent=2) + '\n')
    if cleanup.returncode:
        raise RuntimeError('Could not end smoke-test schroot session')
print(json.dumps({'result': report['result'], 'version': report['version']}))
