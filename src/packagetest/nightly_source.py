"""Prepare independently buildable, pinned snapshot source packages.

Ubuntu's checksum-pinned source package supplies Debian packaging. Upstream Git
supplies the new source tree. This avoids assuming every Ubuntu source has an
up-to-date, publicly usable git-buildpackage branch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import io
import json
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .artifacts import checksum_entries, fields, sha256, verify_source
from .commands import CommandRunner
from .snapshot import build_snapshot
from .snapshot_lock import select_commit

BUILD_REQUIREMENTS = [
    {'name': 'pbr', 'version': '7.0.3', 'sha256': 'ff223894eb1cd271a98076b13d3badff3bb36c424074d26334cd25aebeecea6b'},
    {'name': 'setuptools', 'version': '80.9.0', 'sha256': '062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922'},
    {'name': 'wheel', 'version': '0.45.1', 'sha256': '708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248'},
]
SCM_REQUIREMENTS = [
    {'name': 'setuptools-scm', 'version': '8.3.1', 'sha256': '332ca0d43791b818b841213e76b1971b7711a960761c5bea5fc5cdb5196fbce3'},
    {'name': 'packaging', 'version': '25.0', 'sha256': '29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484'},
]


class Preparation:
    def __init__(self, destination: Path):
        self.root = destination.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.runner = CommandRunner(self.root / 'logs' / 'commands.jsonl', stream=True, timeout=1800)

    def command(self, *args, cwd=None, env=None):
        result = self.runner.run(list(args), cwd or self.work, env_diff=env)
        if result.exit_code:
            raise RuntimeError(f'{args[0]} failed ({result.exit_code}): {result.stderr[-2000:]}')
        return (self.runner.log_path.parent / f'{self.runner.sequence:04d}.stdout.log').read_text().strip()


def download_verified(url: str, destination: Path, digest: str) -> None:
    if urlparse(url).scheme != 'https':
        raise ValueError('Pinned source downloads require HTTPS')
    with urlopen(url, timeout=120) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)
    if sha256(destination) != digest:
        raise ValueError(f'Source checksum mismatch: {destination.name}')


def snapshot_debian_version(archive_version: str, upstream: str) -> str:
    epoch = archive_version.split(':', 1)[0] + ':' if ':' in archive_version else ''
    return epoch + upstream + '-0ubuntu1~hibiscus1'



def ubuntu_maintainer(control: Path) -> None:
    """Record the original Debian maintainer when creating an Ubuntu revision."""
    text = control.read_text()
    source, separator, binaries = text.partition('\n\n')
    match = re.search(r'^Maintainer: (.*(?:\n[ \t].*)*)', source, re.M)
    if not match:
        raise ValueError('Source control lacks Maintainer')
    original = match.group(1)
    if '@ubuntu.com' in original:
        return
    replacement = 'Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>'
    if not re.search(r'^XSBC-Original-Maintainer:', source, re.M):
        replacement += '\nXSBC-Original-Maintainer: ' + original
    source = source[:match.start()] + replacement + source[match.end():]
    control.write_text(source + separator + binaries)


def packaging_adjustments(entry: dict, tree: Path, config_root: Path | None = None) -> list[dict]:
    """Apply only reviewed, checksum-guarded packaging adaptations."""
    config_root = config_root or Path(__file__).resolve().parents[2] / 'config' / 'patches'
    path = config_root / entry['source'] / 'adjustments.json'
    if not path.exists():
        return []
    spec = json.loads(path.read_text())
    if spec['archive_dsc_sha256'] != entry['archive_source']['sha256']:
        raise ValueError('Packaging adaptation requires review for changed archive source')
    applied = []
    for item in spec.get('replace_files', []):
        name = Path(item['name'])
        replacement = Path(item['replacement'])
        if name.is_absolute() or '..' in name.parts or replacement.is_absolute() or '..' in replacement.parts:
            raise ValueError('Unsafe packaging adaptation path')
        original = tree / 'debian' / name
        revised = path.parent / replacement
        if sha256(original) != item['sha256'] or sha256(revised) != item['replacement_sha256']:
            raise ValueError('Packaging adaptation checksum guard failed: ' + str(name))
        shutil.copyfile(revised, original)
        applied.append({'action': 'replace-packaging-file', **item})
    for item in spec.get('add_files', []):
        name, replacement = Path(item['name']), Path(item['replacement'])
        if (not name.parts or not replacement.parts or name.is_absolute() or replacement.is_absolute()
                or '..' in name.parts or '..' in replacement.parts):
            raise ValueError('Unsafe packaging addition path')
        destination = tree / 'debian' / name
        revised = path.parent / replacement
        if any(parent.is_symlink() for parent in [destination, *destination.parents]):
            raise ValueError('Symlink in packaging addition destination')
        if any(parent.is_symlink() for parent in [revised, *revised.parents]):
            raise ValueError('Symlink in packaging addition replacement')
        if destination.exists():
            raise ValueError('Packaging addition destination already exists: ' + str(name))
        if item.get('mode') not in {'0644', '0755'}:
            raise ValueError('Packaging addition requires explicit mode 0644 or 0755')
        if sha256(revised) != item['replacement_sha256']:
            raise ValueError('Packaging addition checksum guard failed: ' + str(name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as handle:
            handle.write(revised.read_bytes())
        destination.chmod(int(item['mode'], 8))
        applied.append({'action': 'add-packaging-file', **item})
    if not spec.get('drop_patches'):
        return applied
    series_path = tree / 'debian' / 'patches' / 'series'
    series = series_path.read_text().splitlines()
    for patch in spec.get('drop_patches', []):
        original = tree / 'debian' / 'patches' / patch['name']
        upstream = tree / patch['upstream_file']
        if sha256(original) != patch['sha256'] or sha256(upstream) != patch['upstream_file_sha256']:
            raise ValueError('Packaging adaptation checksum guard failed: ' + patch['name'])
        matching = [index for index, line in enumerate(series) if line.split() and line.split()[0] == patch['name']]
        if len(matching) != 1:
            raise ValueError('Packaging adaptation requires one matching series entry')
        series[matching[0]] = '# Superseded upstream: ' + patch['name']
        applied.append({'action': 'omit-obsolete-patch', **patch})
    series_path.write_text('\n'.join(series) + '\n')
    return applied


def already_applied_patches(tree: Path) -> list[dict]:
    """Omit a quilt patch only if its complete inverse applies without fuzz.

    Also require that the forward patch does not apply, so ambiguous repeated
    contexts do not qualify. Both probes are dry runs; upstream is unchanged.
    """
    series = tree / 'debian/patches/series'
    if not series.exists():
        return []
    lines = series.read_text().splitlines()
    omitted = []
    for index, line in enumerate(lines):
        words = line.split()
        if not words or words[0].startswith('#') or len(words) != 1:
            continue
        name = Path(words[0])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Unsafe quilt patch path')
        patch = tree / 'debian/patches' / name
        data = patch.read_bytes()
        args = ['patch', '--dry-run', '--force', '--fuzz=0', '-p1']
        reverse = subprocess.run(args + ['--reverse'], input=data, cwd=tree, capture_output=True)
        if reverse.returncode:
            continue
        forward = subprocess.run(args, input=data, cwd=tree, capture_output=True)
        if forward.returncode:
            lines[index] = '# Fully present upstream (verified reverse dry run): ' + str(name)
            omitted.append({'action': 'omit-already-applied-patch', 'name': str(name),
                            'sha256': sha256(patch), 'reverse_probe': reverse.stdout.decode(errors='replace')})
    if omitted:
        series.write_text('\n'.join(lines) + '\n')
    return omitted

def extract_snapshot(archive: Path, destination: Path) -> None:
    destination.mkdir()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if len(roots) != 1:
            raise ValueError('Snapshot archive must contain exactly one top-level directory')
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise ValueError('Unsafe snapshot archive path')
        # Python data filtering prevents symlinks escaping the extraction root.
        source.extractall(destination, filter='data')
    root = destination / next(iter(roots))
    if not root.is_dir():
        raise ValueError('Snapshot archive root is not a directory')
    for child in root.iterdir():
        child.rename(destination / child.name)
    root.rmdir()


def preserve_orig_components(baseline_dsc: Path, source: str, upstream: str,
                             tree: Path, output: Path) -> list[dict]:
    """Carry checksum-pinned supplementary orig tarballs into the new source.

    Debian packaging can depend on components absent from upstream Git, such
    as Horizon's separately archived XStatic assets. Preserve their bytes and
    component layout while naming them for the new snapshot version.
    """
    components = []
    for digest, size, filename in checksum_entries(fields(baseline_dsc)):
        if '.orig-' not in filename or filename.endswith('.asc'):
            continue
        match = re.fullmatch(re.escape(source) + r'_[^/]+\.orig-([A-Za-z0-9-]+)\.tar\.(gz|xz|bz2|lzma)', filename)
        if not match:
            raise ValueError('Unsupported supplementary orig filename: ' + filename)
        component, compression = match.groups()
        archive = baseline_dsc.parent / filename
        if archive.stat().st_size != int(size) or sha256(archive) != digest:
            raise ValueError('Supplementary orig checksum mismatch: ' + filename)
        destination = output / f'{source}_{upstream}.orig-{component}.tar.{compression}'
        component_tree = tree / component
        if destination.exists() or destination.is_symlink() or component_tree.exists() or component_tree.is_symlink():
            raise ValueError('Supplementary orig component collides with snapshot: ' + component)
        extract_snapshot(archive, component_tree)
        shutil.copyfile(archive, destination)
        components.append({'component': component, 'archive_file': filename,
                           'snapshot_file': destination.name, 'sha256': digest})
    return components


def git_archive(build: Preparation, checkout: Path, selected: dict, destination: Path, package: str) -> dict:
    raw = build.work / 'git-source.tar'
    build.command('git', 'archive', '--format=tar', f'--prefix={package}-{selected["upstream_version"]}/',
                  f'--output={raw}', selected['sha'], cwd=checkout)
    epoch = int(selected['commit_timestamp'])
    with tarfile.open(raw) as source, destination.open('wb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target:
                for member in sorted(source.getmembers(), key=lambda item: item.name):
                    member.uid = member.gid = 0
                    member.uname = member.gname = ''
                    member.mtime = epoch
                    member.pax_headers = {}
                    contents = source.extractfile(member).read() if member.isfile() else None
                    target.addfile(member, io.BytesIO(contents) if contents is not None else None)
    return {**selected, 'sdist_sha256': sha256(destination), 'sdist_file': destination.name, 'backend': 'git-archive'}


def prepare_source(entry: dict, destination: Path, *, cutoff: str | None = None) -> dict:
    """Resolve and prepare one source; never overwrite an earlier generation.

    Return absolute ``dsc`` and ``lock`` paths plus metadata. Failure leaves
    resolution.json with FAILED and all command logs for artifact upload.
    """
    build = Preparation(destination)
    report = {'source': entry['source'], 'status': 'PREPARING', 'catalog_entry': entry}
    report_path = build.root / 'resolution.json'
    try:
        if entry.get('discovery_error') or entry.get('upstream_resolution_error'):
            raise ValueError(entry.get('discovery_error') or entry['upstream_resolution_error'])
        if not re.fullmatch(r'[a-f0-9]{40}', entry.get('upstream_sha', '')):
            raise ValueError('Source preparation requires a plan-pinned upstream_sha')
        source = entry['source']
        baseline_dir = build.work / 'baseline'
        baseline_dir.mkdir()
        spec = entry['archive_source']
        baseline_dsc = baseline_dir / Path(urlparse(spec['url']).path).name
        download_verified(spec['url'], baseline_dsc, spec['sha256'])
        for digest, _, filename in checksum_entries(fields(baseline_dsc)):
            download_verified(urljoin(spec['url'], filename), baseline_dir / filename, digest)
        verify_source(baseline_dsc, source, entry['archive_version'])
        packaging_tree = build.work / 'packaging'
        build.command('dpkg-source', '--skip-patches', '-x', str(baseline_dsc), str(packaging_tree))
        checkout = build.work / 'upstream-discovery'
        build.command('git', 'clone', '--no-checkout', entry['upstream_repository'], str(checkout))
        # Stable branch is chosen in the catalog from release declarations. A
        # deleted/missing declared branch is an error, never a master fallback.
        build.command('git', 'cat-file', '-e', entry['upstream_sha'] + '^{commit}', cwd=checkout)
        build.command('git', 'update-ref', 'refs/remotes/origin/packagetest-frozen', entry['upstream_sha'], cwd=checkout)
        selected = select_commit(build, checkout, 'packagetest-frozen', None)
        selected.update(ref=entry['upstream_ref'], cutoff=cutoff)
        if selected['sha'] != entry['upstream_sha']:
            raise ValueError('Source preparation changed the plan-pinned upstream SHA')
        upstream = selected['upstream_version']
        version = snapshot_debian_version(entry['archive_version'], upstream)
        build.command('dpkg', '--compare-versions', version, 'gt', entry['archive_version'])
        orig = build.root / f'{source}_{upstream}.orig.tar.gz'
        if entry.get('snapshot_backend') == 'git-archive':
            snapshot = git_archive(build, checkout, selected, orig, source)
        else:
            snapshot_spec = {**selected, 'repository': entry['upstream_repository'],
                             'build_requirements': BUILD_REQUIREMENTS + (SCM_REQUIREMENTS if entry.get('snapshot_version_backend') == 'setuptools-scm' else []),
                             'version_backend': entry.get('snapshot_version_backend', 'pbr'), 'archive_format': 'portable-v1'}
            snapshot = build_snapshot(build, {'source': source, 'input': {'snapshot': snapshot_spec, 'upstream_version': upstream}}, orig)
        tree = build.root / f'{source}-{upstream}'
        extract_snapshot(orig, tree)
        report['supplementary_orig_components'] = preserve_orig_components(
            baseline_dsc, source, upstream, tree, build.root)
        if (tree / 'debian').exists():
            raise ValueError('Upstream snapshot unexpectedly contains Debian packaging')
        shutil.copytree(packaging_tree / 'debian', tree / 'debian', symlinks=True)
        ubuntu_maintainer(tree / 'debian' / 'control')
        report['packaging_adjustments'] = packaging_adjustments(entry, tree)
        report['packaging_adjustments'].extend(already_applied_patches(tree))
        build.command('dch', '--newversion', version, '--distribution', 'resolute', '--force-distribution',
                      'Nightly OpenStack 2026.2 snapshot from pinned upstream commit ' + selected['sha'] + '.',
                      cwd=tree, env={'DEBFULLNAME': 'Packaging Build Agent', 'DEBEMAIL': 'packaging-agent@example.invalid'})
        build.command('dpkg-buildpackage', '-S', '-d', '-nc', '-us', '-uc', cwd=tree,
                      env={'SOURCE_DATE_EPOCH': selected['commit_timestamp']})
        dscs = list(build.root.glob('*.dsc'))
        if len(dscs) != 1:
            raise ValueError('Expected exactly one generated Debian source package')
        verify_source(dscs[0], source, version)
        report.update(status='PREPARED', version=version, snapshot=snapshot,
                      dsc=str(dscs[0]), dsc_sha256=sha256(dscs[0]),
                      prepared_at=datetime.now(timezone.utc).isoformat())
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        return {'dsc': str(dscs[0]), 'metadata': report, 'lock': str(report_path)}
    except Exception as error:
        report.update(status='FAILED', error=str(error))
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        raise
