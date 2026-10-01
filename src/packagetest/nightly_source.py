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
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .artifacts import checksum_entries, fields, sha256, verify_source
from .catalog import dependency_names
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
    # Ubuntu's canonical maintainer address is hosted on lists.ubuntu.com;
    # individual Ubuntu maintainers commonly use ubuntu.com directly.
    if re.search(r'@(?:[A-Za-z0-9-]+\.)*ubuntu\.com\b', original, re.I):
        return
    replacement = 'Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>'
    if not re.search(r'^XSBC-Original-Maintainer:', source, re.M):
        replacement += '\nXSBC-Original-Maintainer: ' + original
    source = source[:match.start()] + replacement + source[match.end():]
    control.write_text(source + separator + binaries)


def _add_control_dependency(text: str, field: str, package: str) -> str:
    """Insert one unversioned dependency into a multiline control field."""
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith(field + ':')), None)
    if start is None:
        raise ValueError(f'control paragraph has no {field} field')
    end = start + 1
    while end < len(lines) and lines[end].startswith((' ', '\t')):
        end += 1
    current = ''.join(lines[start:end])
    if re.search(rf'(?<![A-Za-z0-9+.-]){re.escape(package)}(?![A-Za-z0-9+.-])', current):
        return text
    insert = end
    for index in range(start + 1, end):
        token = lines[index].strip().split(maxsplit=1)[0].rstrip(',') if lines[index].strip() else ''
        if token.startswith('${') or token.lower() > package:
            insert = index
            break
    lines.insert(insert, f' {package},\n')
    return ''.join(lines)


def upstream_dependency_adjustments(entry: dict, tree: Path) -> list[dict]:
    """Add missing, archive-proven upstream dependencies to the build candidate.

    Test and build-system dependencies are confined to the source build
    dependency field. Runtime metadata is retained in the plan for review but
    is not copied automatically into binary Depends because Debian packaging
    can intentionally split optional runtime features. Every mutation becomes
    part of the downloadable packaging proposal and remains subject to the
    normal rebuild and autopkgtest gates.
    """
    records = entry.get('upstream_dependency_requirements', [])
    usable = [record for record in records
              if record.get('kind') in {'test', 'build-system'}
              and record.get('archive_binary') and record.get('archive_satisfies') is True]
    if not usable:
        return []
    path = tree / 'debian' / 'control'
    before = path.read_text()
    paragraphs = re.split(r'(\n\s*\n)', before)
    source = paragraphs[0]
    build_field = 'Build-Depends-Indep' if 'Build-Depends-Indep:' in source else 'Build-Depends'
    build_values = []
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r'^Build-Depends(?:-Indep|-Arch)?:', line):
            continue
        value = line.split(':', 1)[1]
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].startswith((' ', '\t')):
            value += '\n' + lines[cursor].strip()
            cursor += 1
        build_values.append(value)
    build_names = dependency_names(', '.join(build_values))
    actions = []
    grouped: dict[str, list[dict]] = {}
    for record in usable:
        grouped.setdefault(record['archive_binary'], []).append(record)
    for package, requirements in sorted(grouped.items()):
        kinds = {record['kind'] for record in requirements}
        scopes = []
        if package not in build_names:
            field = 'Build-Depends' if 'build-system' in kinds else build_field
            source = _add_control_dependency(source, field, package)
            build_names.add(package)
            scopes.append('build')
        if scopes:
            evidence = sorted({
                f"{record['file']}:{record['line']} {record['requirement']}"
                for record in requirements
            })
            actions.append({
                'action': 'add-upstream-dependency',
                'package': package,
                'source': requirements[0]['archive_source'],
                'archive_version': requirements[0]['archive_version'],
                'requirement_kinds': sorted(kinds),
                'scopes': scopes,
                'evidence': evidence,
                'reason': ('Pinned upstream dependency is absent from Debian packaging; '
                           'the Ubuntu archive version satisfies the declared constraint.'),
            })
    paragraphs[0] = source
    revised = ''.join(paragraphs)
    if revised != before:
        path.write_text(revised)
    return actions


def packaging_adjustments(entry: dict, tree: Path, config_root: Path | None = None) -> list[dict]:
    """Apply only reviewed, checksum-guarded packaging adaptations."""
    config_root = config_root or Path(__file__).resolve().parents[2] / 'config' / 'patches'
    path = config_root / entry['source'] / 'adjustments.json'
    if not path.exists():
        return []
    spec = json.loads(path.read_text())
    if spec['archive_dsc_sha256'] != entry['archive_source']['sha256']:
        raise ValueError('Packaging adaptation requires review for changed archive source')
    failures = []
    for item in spec.get('replace_files', []):
        name = Path(item['name'])
        replacement = Path(item['replacement'])
        if name.is_absolute() or '..' in name.parts or replacement.is_absolute() or '..' in replacement.parts:
            raise ValueError('Unsafe packaging adaptation path')
        original = tree / 'debian' / name
        revised = path.parent / replacement
        original_digest = sha256(original)
        replacement_digest = sha256(revised)
        if original_digest != item['sha256']:
            failures.append(f'{name} source expected {item["sha256"]}, got {original_digest}')
        if replacement_digest != item['replacement_sha256']:
            failures.append(f'{name} replacement expected {item["replacement_sha256"]}, got {replacement_digest}')
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
        replacement_digest = sha256(revised)
        if replacement_digest != item['replacement_sha256']:
            failures.append(f'{name} addition expected {item["replacement_sha256"]}, got {replacement_digest}')
    original_series = []
    series_path = tree / 'debian' / 'patches' / 'series'
    if spec.get('drop_patches'):
        original_series = series_path.read_text().splitlines()
    for patch in spec.get('drop_patches', []):
        original = tree / 'debian' / 'patches' / patch['name']
        upstream = tree / patch['upstream_file']
        patch_digest = sha256(original)
        upstream_digest = sha256(upstream)
        if patch_digest != patch['sha256']:
            failures.append(f'{patch["name"]} patch expected {patch["sha256"]}, got {patch_digest}')
        if upstream_digest != patch['upstream_file_sha256']:
            failures.append(f'{patch["name"]} upstream {patch["upstream_file"]} expected '
                            f'{patch["upstream_file_sha256"]}, got {upstream_digest}')
        matching = [index for index, line in enumerate(original_series)
                    if line.split() and line.split()[0] == patch['name']]
        if len(matching) != 1:
            failures.append(f'{patch["name"]} expected one active series entry, got {len(matching)}')
    if failures:
        raise ValueError('Packaging adaptation checksum guard failed:\n- ' + '\n- '.join(failures))

    applied = []
    for item in spec.get('replace_files', []):
        name = Path(item['name'])
        shutil.copyfile(path.parent / item['replacement'], tree / 'debian' / name)
        applied.append({'action': 'replace-packaging-file', **item})
    for item in spec.get('add_files', []):
        name, replacement = Path(item['name']), Path(item['replacement'])
        destination = tree / 'debian' / name
        revised = path.parent / replacement
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as handle:
            handle.write(revised.read_bytes())
        destination.chmod(int(item['mode'], 8))
        applied.append({'action': 'add-packaging-file', **item})
    if not spec.get('drop_patches'):
        return applied
    # A reviewed replacement may deliberately add entries to patches/series.
    # Apply obsolete-patch omissions to the post-replacement file so that those
    # entries are not lost by writing the archive's original series back out.
    series = series_path.read_text().splitlines()
    for patch in spec.get('drop_patches', []):
        matching = [index for index, line in enumerate(series) if line.split() and line.split()[0] == patch['name']]
        series[matching[0]] = '# Superseded upstream: ' + patch['name']
        applied.append({'action': 'omit-obsolete-patch', **patch})
    series_path.write_text('\n'.join(series) + '\n')
    return applied


def write_packaging_proposal(entry: dict, baseline: Path, tree: Path, output: Path,
                             actions: list[dict]) -> dict | None:
    """Export temporary packaging adaptations for human review upstream.

    The patch is rooted at ``debian/`` so it can be reviewed against a real
    packaging repository.  The archive source remains the reproducible base;
    choosing Debian or Ubuntu as the submission target is deliberately left to
    a human reviewer.
    """
    if not actions:
        return None
    repositories = []
    for role, key in [('ubuntu', 'packaging_repository'), ('archive', 'archive_packaging_repository')]:
        repository = entry.get(key)
        if repository and repository not in {item['repository'] for item in repositories}:
            repositories.append({'role': role, 'repository': repository})
    if not repositories:
        raise ValueError('Packaging proposal requires a destination repository candidate')
    branches = entry.get('packaging_branch_candidates') or []
    if not branches:
        raise ValueError('Packaging proposal requires destination branch candidates')

    output.mkdir(parents=True, exist_ok=True)
    patch_path = output / 'packaging-proposal.patch'
    with tempfile.TemporaryDirectory(prefix='packagetest-proposal-') as temporary:
        repository = Path(temporary) / 'repository'
        repository.mkdir()
        shutil.copytree(baseline, repository / 'debian', symlinks=True)
        environment = {**os.environ,
                       'GIT_AUTHOR_NAME': 'Packaging Build Agent',
                       'GIT_AUTHOR_EMAIL': 'packaging-agent@example.invalid',
                       'GIT_COMMITTER_NAME': 'Packaging Build Agent',
                       'GIT_COMMITTER_EMAIL': 'packaging-agent@example.invalid',
                       'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+00:00',
                       'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+00:00'}
        subprocess.run(['git', 'init', '--quiet'], cwd=repository, env=environment, check=True)
        subprocess.run(['git', 'add', '--all'], cwd=repository, env=environment, check=True)
        subprocess.run(['git', 'commit', '--quiet', '--no-gpg-sign', '-m', 'archive packaging baseline'],
                       cwd=repository, env=environment, check=True)
        shutil.rmtree(repository / 'debian')
        shutil.copytree(tree / 'debian', repository / 'debian', symlinks=True)
        subprocess.run(['git', 'add', '--all'], cwd=repository, env=environment, check=True)
        result = subprocess.run(['git', 'diff', '--cached', '--binary', '--full-index', 'HEAD', '--', 'debian'],
                                cwd=repository, env=environment, text=True, capture_output=True, check=True)
        if not result.stdout:
            return None
        patch_path.write_text(result.stdout)

    proposal = {
        'schema_version': 1,
        'source': entry['source'],
        'status': 'candidate',
        'human_review_required': True,
        'selected_target': None,
        'destination_candidates': repositories,
        'branch_candidates': branches,
        'archive_base': {
            'version': entry['archive_version'],
            'url': entry['archive_source']['url'],
            'sha256': entry['archive_source']['sha256'],
            'generic_normalization_before_diff': ['ubuntu-maintainer'],
        },
        'patch': {'file': patch_path.name, 'sha256': sha256(patch_path)},
        'actions': actions,
        'removal_condition': 'Retire after the reviewed change is accepted in the selected packaging branch.',
    }
    (output / 'packaging-proposal.json').write_text(json.dumps(proposal, indent=2) + '\n')
    return proposal


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


def prepare_source(entry: dict, destination: Path, *, cutoff: str | None = None,
                   remediation_patch: Path | None = None) -> dict:
    """Resolve and prepare one source; never overwrite an earlier generation.

    Return absolute ``dsc`` and ``lock`` paths plus metadata. Failure leaves
    resolution.json with FAILED and all command logs for artifact upload.
    """
    build = Preparation(destination)
    report = {'source': entry['source'], 'status': 'PREPARING', 'catalog_entry': entry}
    report_path = build.root / 'resolution.json'
    try:
        if (entry.get('discovery_error') or entry.get('upstream_resolution_error') or
                entry.get('upstream_dependency_resolution_error')):
            raise ValueError(entry.get('discovery_error') or entry.get('upstream_resolution_error') or
                             entry['upstream_dependency_resolution_error'])
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
        proposal_baseline = build.work / 'packaging-proposal-baseline'
        shutil.copytree(tree / 'debian', proposal_baseline, symlinks=True)
        report['packaging_adjustments'] = packaging_adjustments(entry, tree)
        report['packaging_adjustments'].extend(already_applied_patches(tree))
        report['packaging_adjustments'].extend(upstream_dependency_adjustments(entry, tree))
        if remediation_patch is not None:
            from .failure_analysis import validate_source_patch
            patch = remediation_patch.read_text()
            validation = validate_source_patch(patch, tree, apply=True)
            if validation['result'] != 'APPLIED':
                raise ValueError('Remediation patch rejected: ' + validation['error'])
            report['remediation'] = {
                'patch_file': remediation_patch.name,
                'patch_sha256': validation['patch_sha256'],
                'paths': validation['paths'],
            }
            report['packaging_adjustments'].append({
                'action': 'local-ai-remediation',
                'patch_file': remediation_patch.name,
                'patch_sha256': validation['patch_sha256'],
                'paths': validation['paths'],
            })
        proposal = write_packaging_proposal(
            entry, proposal_baseline, tree, build.root / 'packaging-proposal',
            report['packaging_adjustments'])
        if proposal:
            report['packaging_proposal'] = proposal
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
