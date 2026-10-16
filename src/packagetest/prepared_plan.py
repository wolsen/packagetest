"""Build an exact candidate dependency graph from prepared Debian sources."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse

from .artifacts import checksum_entries, sha256, verify_source
from .contracts import BuildTarget, PreparedSource
from .relations import BuildContext, is_deferred_relation, parse_relation


@dataclass
class DependencyPlan:
    """Candidate graph and the evidence explaining its selected edges."""

    dependencies: dict[str, list[str]]
    external_decisions: list[dict]
    errors: list[dict]
    reasons: dict[tuple[str, str], list[dict]]

    def __post_init__(self) -> None:
        for source, producers in self.dependencies.items():
            if source in producers or set(producers) - self.dependencies.keys():
                raise ValueError(f'Invalid candidate dependency edges for {source}')


def stable_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def load_prepared(root: Path, catalog: dict) -> dict[str, dict]:
    """Validate one and only one immutable source manifest per catalog entry."""
    entries = {entry['source']: entry for entry in catalog['packages']}
    manifests = {}
    for path in sorted(root.rglob('prepared-source.json')):
        manifest = json.loads(path.read_text())
        identity = PreparedSource.from_mapping(manifest)
        source = identity.source
        if source not in entries:
            raise ValueError(f'Prepared manifest names unknown source: {source!r}')
        if source in manifests:
            raise ValueError(f'Duplicate prepared source manifest: {source}')
        if manifest.get('ci') != catalog.get('ci'):
            raise ValueError(f'Prepared source belongs to another run: {source}')
        entry = entries[source]
        if manifest.get('catalog_entry_sha256') != stable_digest(entry):
            raise ValueError(f'Prepared source catalog identity changed: {source}')
        if manifest.get('pins') != {'upstream_sha': entry.get('upstream_sha'),
                                    'packaging_sha': entry.get('packaging_sha')}:
            raise ValueError(f'Prepared source Git pins changed: {source}')
        source_dir = path.parent / 'source'
        dsc = identity.verified_path(source_dir)
        if not dsc.is_file() or sha256(dsc) != manifest['dsc_sha256']:
            raise ValueError(f'Prepared source .dsc checksum mismatch: {source}')
        dsc_fields = verify_source(dsc, source, manifest['version'])
        referenced = {dsc.name, *(name for _, _, name in checksum_entries(dsc_fields))}
        recorded = {item['file'] for item in manifest.get('source_artifacts', [])}
        if referenced != recorded:
            raise ValueError(f'Prepared source artifact inventory mismatch: {source}')
        for item in manifest['source_artifacts']:
            artifact = source_dir / item['file']
            if not artifact.is_file() or artifact.is_symlink() or sha256(artifact) != item['sha256']:
                raise ValueError(f'Prepared source artifact changed: {source}/{item["file"]}')
        manifests[source] = {**manifest, 'artifact_root': str(path.parent.resolve())}
    missing = entries.keys() - manifests.keys()
    if missing:
        raise ValueError(f'Missing prepared source manifests: {sorted(missing)}')
    return manifests


def _split_relations(value: str, separator: str) -> list[str]:
    result, current, round_depth, square_depth = [], [], 0, 0
    for character in value:
        if character == '(':
            round_depth += 1
        elif character == ')':
            round_depth -= 1
        elif character == '[':
            square_depth += 1
        elif character == ']':
            square_depth -= 1
        if character == separator and not (round_depth or square_depth):
            item = ''.join(current).strip()
            if item:
                result.append(item)
            current = []
        else:
            current.append(character)
    item = ''.join(current).strip()
    if item:
        result.append(item)
    return result


def relation_groups(value: str, context: BuildContext | None = None) -> list[list[dict]]:
    context = context or BuildContext()
    groups = []
    for expression in _split_relations(value, ','):
        alternatives = []
        for alternative in _split_relations(expression, '|'):
            if is_deferred_relation(alternative):
                continue
            relation = parse_relation(alternative)
            if not relation.applies(context):
                continue
            alternatives.append({'package': relation.package,
                                 'operator': relation.operator, 'version': relation.version,
                                 'expression': relation.expression, 'group': expression})
        if alternatives:
            groups.append(alternatives)
    return groups


def satisfies(version: str | None, relation: dict) -> bool:
    if version is None:
        return False
    if not relation.get('operator'):
        return True
    # Debian substvars are expanded by dpkg during the binary build.  They are
    # not versions and passing one to dpkg --compare-versions both emits a
    # confusing diagnostic and cannot improve the pre-build graph decision.
    if '${' in relation['version']:
        return True
    return subprocess.run(['dpkg', '--compare-versions', version,
                           relation['operator'], relation['version']]).returncode == 0


def _published_archive_versions(packages: list[dict]) -> dict[str, list[dict]]:
    """Use binary publication evidence without importing packaging-source versions."""
    return {item['binary']: [dict(source=item['source'], version=item['version'])] for item in packages}


def archive_versions(catalog: dict) -> dict[str, list[dict]]:
    """Prefer published binary versions; retain compatibility with older frozen catalogs."""
    if 'archive_binary_packages' in catalog:
        return _published_archive_versions(catalog['archive_binary_packages'])

    return _legacy_archive_versions(catalog)


def _legacy_archive_versions(catalog: dict) -> dict[str, list[dict]]:
    """Read older catalog fixtures without binary publication records."""
    result: dict[str, list[dict]] = {}
    for entry in catalog['packages']:
        for binary in entry.get('binaries', []):
            result.setdefault(binary, []).append({'source': entry['source'],
                                                  'version': entry['archive_version']})
    for item in catalog.get('archive_python_packages', []):
        record = {'source': item['source'], 'version': item['version']}
        if record not in result.setdefault(item['binary'], []):
            result[item['binary']].append(record)
    return result


def dependency_inputs(manifest: dict) -> list[tuple[str, str, str]]:
    result = []
    for field, value in manifest.get('build_depends', {}).items():
        if value:
            result.append(('build', field, value))
    if manifest.get('testsuite_triggers'):
        result.append(('test', 'Testsuite-Triggers', manifest['testsuite_triggers']))
    for test in manifest.get('autopkgtests', []):
        if test.get('depends'):
            result.append(('test', f"debian/tests/control:{test.get('tests', 'unnamed')}",
                           test['depends']))
    for binary in manifest.get('binary_packages', []):
        for field in ('depends', 'pre_depends'):
            if binary.get(field):
                result.append(('runtime', f"{binary['package']}:{field}", binary[field]))
    return result


def exact_dependencies(catalog: dict, manifests: dict[str, dict], *,
                       context: BuildContext | None = None) -> DependencyPlan:
    if context is None:
        target = catalog.get('target')
        context = BuildTarget.from_mapping(target).context if target else BuildContext()
    providers = {}
    for source, manifest in manifests.items():
        for binary in manifest['binaries']:
            if binary in providers and providers[binary] != source:
                raise ValueError(f'Ambiguous prepared binary provider {binary}: '
                                 f'{providers[binary]}, {source}')
            providers[binary] = source
    archive = archive_versions(catalog)
    dependencies, external, errors = {source: set() for source in manifests}, [], []
    reasons: dict[tuple[str, str], list[dict]] = {}
    for source, manifest in sorted(manifests.items()):
        for phase, location, value in dependency_inputs(manifest):
            for alternatives in relation_groups(value, context):
                candidate_options = []
                for relation in alternatives:
                    provider = providers.get(relation['package'])
                    if provider and provider != source and satisfies(manifests[provider]['version'], relation):
                        candidate_options.append((provider, relation))
                if candidate_options:
                    provider, relation = candidate_options[0]
                    dependencies[source].add(provider)
                    reasons.setdefault((source, provider), []).append({
                        'phase': phase, 'location': location,
                        'binary': relation['package'], 'relation': relation['group'],
                    })
                    continue
                archive_options = [
                    {**record, 'binary': relation['package'], 'relation': relation['group']}
                    for relation in alternatives for record in archive.get(relation['package'], [])
                    if satisfies(record['version'], relation)
                ]
                candidate_names = sorted({providers[relation['package']]
                                          for relation in alternatives
                                          if relation['package'] in providers
                                          and providers[relation['package']] != source})
                if archive_options:
                    external.append({'consumer': source, 'phase': phase, 'location': location,
                                     'decision': 'ubuntu-archive', **archive_options[0]})
                elif candidate_names:
                    errors.append({'source': source, 'phase': phase, 'location': location,
                                   'relation': alternatives[0]['group'],
                                   'candidate_sources': candidate_names,
                                   'error': 'Prepared candidate and known Ubuntu versions do not satisfy the relation'})
                else:
                    names = sorted({relation['package'] for relation in alternatives})
                    external.append({'consumer': source, 'phase': phase, 'location': location,
                                     'decision': 'system-archive-resolution',
                                     'relation': alternatives[0]['group'], 'binaries': names})
    return DependencyPlan({source: sorted(values) for source, values in dependencies.items()},
                          external, errors, reasons)


def apply_prepared_metadata(catalog: dict, manifests: dict[str, dict]) -> dict:
    catalog = json.loads(json.dumps(catalog))
    for entry in catalog['packages']:
        manifest = manifests[entry['source']]
        entry.update(
            archive_binaries=entry.get('binaries', []),
            binaries=manifest['binaries'],
            prepared_version=manifest['version'],
            prepared_source={
                'artifact': f"prepared-source-{entry['source']}",
                'dsc': manifest['dsc'], 'dsc_sha256': manifest['dsc_sha256'],
                'catalog_entry_sha256': manifest['catalog_entry_sha256'],
            },
            exact_build_depends=manifest['build_depends'],
            exact_autopkgtests=manifest['autopkgtests'],
            exact_binary_packages=manifest['binary_packages'],
        )
        packaging_source = manifest.get('packaging_source')
        if packaging_source:
            entry['planned_packaging_source_role'] = entry.get('packaging_source_role')
            entry['packaging_source_role'] = packaging_source.get('role')
            entry['effective_packaging_source'] = packaging_source
            hostname = urlparse(packaging_source.get('url', '')).hostname or ''
            uses_debian = (
                packaging_source.get('role') in {'series-archive-vcs', 'archive-vcs'} or
                hostname == 'debian.org' or hostname.endswith('.debian.org'))
            if not uses_debian:
                entry.pop('packaging_fallback_warning', None)
    return catalog
