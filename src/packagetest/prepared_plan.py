"""Build an exact candidate dependency graph from prepared Debian sources."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
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


def _validate_manifest_identity(manifest: dict, source: str, entries: dict, manifests: dict, catalog: dict) -> None:
    """Require a unique catalog source with matching run identity and pinned inputs."""
    if source not in entries:
        raise ValueError(f'Prepared manifest names unknown source: {source!r}')

    if source in manifests:
        raise ValueError(f'Duplicate prepared source manifest: {source}')

    _validate_manifest_provenance(manifest, source, entries, catalog)


def _validate_manifest_provenance(manifest: dict, source: str, entries: dict, catalog: dict) -> None:
    """Verify the run, catalog hash, and Git pins before accepting prepared content."""
    if manifest.get('ci') != catalog.get('ci'):
        raise ValueError(f'Prepared source belongs to another run: {source}')

    entry = entries[source]
    if manifest.get('catalog_entry_sha256') != stable_digest(entry):
        raise ValueError(f'Prepared source catalog identity changed: {source}')

    if manifest.get('pins') != {'upstream_sha': entry.get('upstream_sha'),
                                'packaging_sha': entry.get('packaging_sha')}:
        raise ValueError(f'Prepared source Git pins changed: {source}')


def verify_prepared_bundle(path: Path, identity: PreparedSource, manifest: dict) -> None:
    """Verify descriptor bytes and every source artifact before recording its root."""
    source = identity.source
    source_dir = path.parent / 'source'
    dsc = identity.verified_path(source_dir)
    if not dsc.is_file() or sha256(dsc) != manifest['dsc_sha256']:
        raise ValueError(f'Prepared source .dsc checksum mismatch: {source}')

    dsc_fields = verify_source(dsc, source, manifest['version'])
    _verify_inventory(dsc, dsc_fields, manifest, source)
    for item in manifest['source_artifacts']:
        _verify_prepared_artifact(source_dir, source, item)


def _verify_prepared_artifact(source_dir: Path, source: str, item: dict) -> None:
    """Reject missing, symlinked, or changed source artifacts."""
    artifact = source_dir / item['file']
    if not artifact.is_file() or artifact.is_symlink() or sha256(artifact) != item['sha256']:
        raise ValueError(f'Prepared source artifact changed: {source}/{item["file"]}')


def _verify_inventory(dsc: Path, dsc_fields: dict, manifest: dict, source: str) -> None:
    """Require exact agreement between descriptor references and recorded artifact names."""
    referenced = {dsc.name, *(name for _, _, name in checksum_entries(dsc_fields))}
    recorded = {item['file'] for item in manifest.get('source_artifacts', [])}
    if referenced != recorded:
        raise ValueError(f'Prepared source artifact inventory mismatch: {source}')


def load_prepared(root: Path, catalog: dict) -> dict[str, dict]:
    """Validate one and only one immutable source manifest per catalog entry."""
    entries = {entry['source']: entry for entry in catalog['packages']}
    manifests = {}
    for path in sorted(root.rglob('prepared-source.json')):
        manifest = json.loads(path.read_text())
        identity = PreparedSource.from_mapping(manifest)
        source = identity.source
        _validate_manifest_identity(manifest, source, entries, manifests, catalog)
        verify_prepared_bundle(path, identity, manifest)
        manifests[source] = {**manifest, 'artifact_root': str(path.parent.resolve())}

    missing = entries.keys() - manifests.keys()
    if missing:
        raise ValueError(f'Missing prepared source manifests: {sorted(missing)}')

    return manifests


class RelationSplitter:
    """Track version and architecture nesting while splitting Debian relation text."""

    def __init__(self, separator: str) -> None:
        """Start one split with independent parenthesis and bracket depths."""
        self.separator = separator
        self.depth = {'round': 0, 'square': 0}
        self.current: list[str] = []
        self.result: list[str] = []

    def consume(self, character: str) -> None:
        """Update nesting before deciding whether this separator ends an item."""
        nesting = {'(': ('round', 1), ')': ('round', -1),
                   '[': ('square', 1), ']': ('square', -1)}.get(character)
        if nesting is not None:
            kind, change = nesting
            self.depth[kind] += change

        if character == self.separator and not any(self.depth.values()):
            self.finish_item()
        else:
            self.current.append(character)

    def finish_item(self) -> None:
        """Append a nonempty trimmed item and reset the current text buffer."""
        item = ''.join(self.current).strip()
        if item:
            self.result.append(item)

        self.current = []


def _split_relations(value: str, separator: str) -> list[str]:
    """Split separators outside version and architecture restrictions."""
    splitter = RelationSplitter(separator)
    for character in value:
        splitter.consume(character)

    splitter.finish_item()
    return splitter.result


def _active_alternatives(expression: str, context: BuildContext) -> list[dict]:
    """Keep concrete alternatives active for the target architecture and profiles."""
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

    return alternatives


def relation_groups(value: str, context: BuildContext | None = None) -> list[list[dict]]:
    """Parse active non-deferred relation alternatives for the build target."""
    context = context or BuildContext()
    groups = []
    for expression in _split_relations(value, ','):
        alternatives = _active_alternatives(expression, context)
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


def _autopkgtest_inputs(manifest: dict) -> list[tuple[str, str, str]]:
    """Read declared test relationships with their control stanza locations."""
    result = []
    for test in manifest.get('autopkgtests', []):
        if test.get('depends'):
            result.append(('test', f"debian/tests/control:{test.get('tests', 'unnamed')}",
                           test['depends']))

    return result


def _runtime_inputs(manifest: dict) -> list[tuple[str, str, str]]:
    """Read Depends and Pre-Depends from every declared binary package."""
    result = []
    for binary in manifest.get('binary_packages', []):
        for field in ('depends', 'pre_depends'):
            if binary.get(field):
                result.append(('runtime', f"{binary['package']}:{field}", binary[field]))

    return result


def dependency_inputs(manifest: dict) -> list[tuple[str, str, str]]:
    """Collect phase-tagged dependency declarations in established evaluation order."""
    result = []
    for field, value in manifest.get('build_depends', {}).items():
        if value:
            result.append(('build', field, value))

    if manifest.get('testsuite_triggers'):
        result.append(('test', 'Testsuite-Triggers', manifest['testsuite_triggers']))

    result.extend(_autopkgtest_inputs(manifest))
    result.extend(_runtime_inputs(manifest))
    return result


def _prepared_providers(manifests: dict[str, dict]) -> dict[str, str]:
    """Map exact binary names to unique prepared source providers."""
    providers = {}
    for source, manifest in manifests.items():
        for binary in manifest['binaries']:
            if binary in providers and providers[binary] != source:
                raise ValueError(f'Ambiguous prepared binary provider {binary}: '
                                 f'{providers[binary]}, {source}')

            providers[binary] = source

    return providers


def _dependency_context(catalog: dict, context: BuildContext | None) -> BuildContext:
    """Use an explicit build context or derive it from the frozen target."""
    if context is not None:
        return context

    target = catalog.get('target')
    return BuildTarget.from_mapping(target).context if target else BuildContext()


class ExactDependencyResolver:
    """Own provider evidence and graph decisions for one prepared catalog."""

    def __init__(self, catalog: dict, manifests: dict[str, dict], context: BuildContext) -> None:
        """Validate provider uniqueness and initialize per-source decision collections."""
        self.manifests = deepcopy(manifests)
        self.context = context
        self.providers = _prepared_providers(manifests)
        self.archive = deepcopy(archive_versions(catalog))
        self.dependencies: dict[str, set[str]] = {source: set() for source in manifests}
        self.external: list[dict] = []
        self.errors: list[dict] = []
        self.reasons: dict[tuple[str, str], list[dict]] = {}
        self._resolved: DependencyPlan | None = None

    def resolve(self) -> DependencyPlan:
        """Resolve the captured inputs once and return an independent plan on every call."""
        if self._resolved is None:
            for source, manifest in sorted(self.manifests.items()):
                self._source(source, manifest)

            self._resolved = DependencyPlan(
                {source: sorted(values) for source, values in self.dependencies.items()},
                self.external, self.errors, self.reasons)

        return deepcopy(self._resolved)

    def _source(self, source: str, manifest: dict) -> None:
        """Resolve active relation groups across build, test, and runtime phases."""
        for phase, location, value in dependency_inputs(manifest):
            for alternatives in relation_groups(value, self.context):
                self._group(source, phase, location, alternatives)

    def _candidate_options(self, source: str, alternatives: list[dict]) -> list[tuple[str, dict]]:
        """Prefer satisfying prepared producers in the declared alternative order."""
        options = []
        for relation in alternatives:
            provider = self.providers.get(relation['package'])
            if provider and provider != source and satisfies(self.manifests[provider]['version'], relation):
                options.append((provider, relation))

        return options

    def _archive_options(self, alternatives: list[dict]) -> list[dict]:
        """Find satisfying published archive versions in relation and provider order."""
        return [{**record, 'binary': relation['package'], 'relation': relation['group']}
                for relation in alternatives for record in self.archive.get(relation['package'], [])
                if satisfies(record['version'], relation)]

    def _candidate_names(self, source: str, alternatives: list[dict]) -> list[str]:
        """Identify selected producers even when their prepared versions cannot satisfy a group."""
        return sorted({self.providers[relation['package']] for relation in alternatives
                       if relation['package'] in self.providers and self.providers[relation['package']] != source})

    def _group(self, source: str, phase: str, location: str, alternatives: list[dict]) -> None:
        """Prefer a prepared edge before considering archive or unresolved system dependencies."""
        candidates = self._candidate_options(source, alternatives)
        if candidates:
            provider, relation = candidates[0]
            self.dependencies[source].add(provider)
            self.reasons.setdefault((source, provider), []).append({
                'phase': phase, 'location': location,
                'binary': relation['package'], 'relation': relation['group'],
            })
            return

        self._external_group(source, phase, location, alternatives)

    def _external_group(self, source: str, phase: str, location: str, alternatives: list[dict]) -> None:
        """Distinguish proven archive satisfaction from candidate errors and system resolution."""
        archive_options = self._archive_options(alternatives)
        candidate_names = self._candidate_names(source, alternatives)
        if archive_options:
            self.external.append({'consumer': source, 'phase': phase, 'location': location,
                                  'decision': 'ubuntu-archive', **archive_options[0]})
        elif candidate_names:
            self.errors.append({'source': source, 'phase': phase, 'location': location,
                                'relation': alternatives[0]['group'], 'candidate_sources': candidate_names,
                                'error': 'Prepared candidate and known Ubuntu versions do not satisfy the relation'})
        else:
            names = sorted({relation['package'] for relation in alternatives})
            self.external.append({'consumer': source, 'phase': phase, 'location': location,
                                  'decision': 'system-archive-resolution',
                                  'relation': alternatives[0]['group'], 'binaries': names})


def exact_dependencies(catalog: dict, manifests: dict[str, dict], *,
                       context: BuildContext | None = None) -> DependencyPlan:
    """Resolve exact dependencies without inferring binaries from packaging-source versions."""
    return ExactDependencyResolver(catalog, manifests, _dependency_context(catalog, context)).resolve()


def apply_prepared_metadata(catalog: dict, manifests: dict[str, dict]) -> dict:
    """Copy the catalog and replace baseline declarations with exact prepared metadata."""
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
        _effective_packaging_source(entry, manifest)

    return catalog


def _effective_packaging_source(entry: dict, manifest: dict) -> None:
    """Replace planned provenance and retain warnings only for effective Debian packaging."""
    packaging_source = manifest.get('packaging_source')
    if packaging_source:
        entry['planned_packaging_source_role'] = entry.get('packaging_source_role')
        entry['packaging_source_role'] = packaging_source.get('role')
        entry['effective_packaging_source'] = packaging_source
        uses_debian = _uses_debian_packaging(packaging_source)
        if not uses_debian:
            entry.pop('packaging_fallback_warning', None)


def _uses_debian_packaging(packaging_source: dict) -> bool:
    """Recognize Debian packaging roles and repository hosts."""
    hostname = urlparse(packaging_source.get('url', '')).hostname or ''
    return (
        packaging_source.get('role') in {'series-archive-vcs', 'archive-vcs'} or
        hostname == 'debian.org' or hostname.endswith('.debian.org'))
