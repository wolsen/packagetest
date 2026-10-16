"""Build one frozen catalog snapshot with verified same-run dependencies."""
import argparse
import json
from pathlib import Path
import sys
import traceback

from packagetest.artifacts import fields, sha256
from packagetest.contracts import BuildTarget, PreparedSource
from packagetest.prepared_plan import relation_groups
from packagetest.relations import BuildContext
from packagetest.handoff import build_repository, collect_producers, stamp_generation
from packagetest.locked import LockedBuild
from packagetest.snapshot_targets import build_target, catalog_target


def dependencies(entries, source):
    result = set()
    def visit(name):
        for dep in entries[name].get('run_dependencies', []):
            if dep not in result:
                result.add(dep)
                visit(dep)
    visit(source)
    return result


def required_versions(source_fields: dict, producers: dict, context: BuildContext | None = None) -> dict[str, str]:
    """Select exact producer versions from the adapted source's dependencies."""
    names = _required_binary_names(source_fields, context)
    return {binary['package']: binary['version'] for binaries in producers.values()
            for binary in binaries if binary['package'] in names}


def validate_candidate_dependencies(entry: dict, entries: dict, source_fields: dict,
                                    required: set[str], context: BuildContext) -> None:
    """A repair may not silently add edges to the frozen build graph.

    Existing archive bootstrap edges are intentional. A newly required
    selected producer needs a fresh plan, even when its archive version exists.
    """
    relationships = ', '.join(source_fields.get(key, '') for key in
                              ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch'))
    providers = {binary: name for name, candidate in entries.items()
                 for binary in candidate.get('binaries', [])}
    allowed = required | set(entry.get('archive_bootstrap_dependencies', [])) | {entry['source']}
    original = ', '.join(entry.get('exact_build_depends', {}).values())
    original_groups = {group[0]['group'] for group in relation_groups(original, context)}
    _validate_new_groups(relationships, context, original_groups, providers, allowed)


def declared_binaries(source_fields):
    """Use the prepared source package, rather than archive-era metadata."""
    binaries = sorted(name.strip() for name in source_fields.get('Binary', '').split(',')
                      if name.strip())
    if not binaries:
        raise ValueError('Prepared source .dsc declares no binary packages')
    return binaries


def _required_binary_names(source_fields: dict, context: BuildContext | None) -> set[str]:
    """Resolve active source build-dependency binary names."""
    relationships = ', '.join(source_fields.get(key, '') for key in
                              ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch'))
    names = {item['package'] for group in relation_groups(relationships, context) for item in group}
    return names


def _validate_new_groups(relationships: str, context: BuildContext, original_groups: set[str], providers: dict, allowed: set[str]) -> None:
    """Require newly introduced relation groups to stay within the frozen producer graph."""
    for alternatives in relation_groups(relationships, context):
        if alternatives[0]['group'] in original_groups:
            continue  # The frozen planner already decided how to satisfy this group.

        candidates = _group_providers(alternatives, providers)
        if candidates and not candidates & allowed:
            raise ValueError('Source dependencies changed the frozen candidate graph; '
                             f'replan required for {sorted(candidates)}')


def _group_providers(alternatives: list[dict], providers: dict) -> set[str]:
    """Find selected producers named by a dependency alternative group."""
    candidates = {providers[item['package']] for item in alternatives
                  if item['package'] in providers}
    return candidates


def _execute_build(args: argparse.Namespace, state: dict) -> int:
    """Run frozen dependency handoff before any source preparation or build commands."""
    snapshot_target, target, identity, entries, entry, required = _build_inputs(args)
    locks = _dependency_locks(args.inputs, entries, required)
    if required - locks.keys():
        state['result'] = 'BLOCKED'
        raise ValueError(f'Missing dependency builds: {sorted(required - locks.keys())}')

    producers = collect_producers(args.inputs, locks, run_id=args.run_id,
                                  run_attempt=args.run_attempt, target=target)
    if args.check_dependencies_only:
        state['result'] = 'READY'
        return 0

    if producers:
        build_repository(producers, args.output / 'dependency-repository')

    state['stage'] = 'snapshot-source'
    prepared, dsc = _prepare_build_source(args, entry, identity, snapshot_target)
    lock = _prepared_build_lock(args, entry, entries, required, target, producers, prepared, dsc)
    state['stage'] = 'binary-build'
    build = LockedBuild(lock, args.output, prepared_sources={args.source: dsc},
                        external_artifacts=producers, timeout=5400)
    result = build.run()
    stamp_generation(build.root / 'generation-manifest.json', run_id=args.run_id, run_attempt=args.run_attempt)
    _record_build_result(build, state)
    return result


def _dependency_locks(inputs: Path, entries: dict, required: set[str]) -> dict:
    """Load one exact frozen catalog lock for each available producer."""
    locks = {}
    for path in inputs.rglob('build-lock.json'):
        lock = json.loads(path.read_text())
        name = lock.get('catalog_entry', {}).get('source')
        if name in required:
            if name in locks:
                raise ValueError(f'Duplicate dependency lock: {name}')

            if lock['catalog_entry'] != entries[name]:
                raise ValueError(f'Dependency input differs from frozen catalog: {name}')

            locks[name] = lock

    return locks


def _prepare_build_source(args: argparse.Namespace, entry: dict, identity: dict, snapshot_target: dict) -> tuple[dict, Path]:
    """Reuse verified prepared source or prepare fresh source for remediation."""
    if args.prepared_source:
        prepared, dsc = _reuse_prepared_source(args, entry, identity)
    else:
        # The dependency-only preflight deliberately runs before host build
        # tools are installed. Keep source-preparation dependencies out of
        # that path; the complete build runs after prepare-builder.sh.
        from packagetest.nightly_source import prepare_source
        prepared = prepare_source(entry, args.output / 'source-preparation',
                                  remediation_patch=args.remediation_patch,
                                  target=snapshot_target)
        dsc = Path(prepared['dsc'])

    return prepared, dsc


def _reuse_prepared_source(args: argparse.Namespace, entry: dict, identity: dict) -> tuple[dict, Path]:
    """Verify frozen manifest identity and descriptor before reusing a prepared bundle."""
    if args.remediation_patch:
        raise ValueError('A remediation patch requires fresh source preparation')

    prepared_manifest = json.loads(
        (args.prepared_source / 'prepared-source.json').read_text())
    _validate_prepared_manifest(prepared_manifest, args, entry, identity)
    prepared_identity = PreparedSource.from_mapping(prepared_manifest)
    dsc = prepared_identity.verified_path(args.prepared_source / 'source')
    if not dsc.is_file() or sha256(dsc) != prepared_manifest['dsc_sha256']:
        raise ValueError('Prepared source .dsc is missing or changed')

    resolution = args.prepared_source / 'resolution.json'
    prepared = {'dsc': str(dsc),
                'metadata': json.loads(resolution.read_text()) if resolution.is_file() else {}}
    return prepared, dsc


def _validate_prepared_manifest(prepared_manifest: dict, args: argparse.Namespace, entry: dict, identity: dict) -> None:
    """Require both run identity and final-plan content hashes to match."""
    if (prepared_manifest.get('source') != args.source
            or prepared_manifest.get('ci') != identity):
        raise ValueError('Prepared source identity differs from this build')

    expected = entry.get('prepared_source', {})
    if (prepared_manifest.get('dsc_sha256') != expected.get('dsc_sha256')
            or prepared_manifest.get('catalog_entry_sha256') !=
            expected.get('catalog_entry_sha256')):
        raise ValueError('Prepared source differs from the final dependency plan')


def _prepared_build_lock(args: argparse.Namespace, entry: dict, entries: dict, required: set[str], target: dict, producers: dict, prepared: dict, dsc: Path) -> dict:
    """Build a lock using adapted source declarations and exact producer versions."""
    source_fields = fields(dsc)
    version = source_fields['Version']
    if args.prepared_source and version != entry.get('prepared_version'):
        raise ValueError('Prepared source version differs from the final dependency plan')

    # Reviewed packaging adaptations can add or tighten dependencies. Pin
    # the dependencies of the source we actually build, not its baseline.
    context = BuildTarget.from_mapping(target).context
    if args.remediation_patch:
        validate_candidate_dependencies(entry, entries, source_fields, required, context)

    versions = required_versions(source_fields, producers, context)
    expected_binaries = declared_binaries(source_fields)
    package = {'source': args.source, 'version': version,
               'expected_binaries': expected_binaries,
               'external_dependencies': sorted(producers), 'required_build_versions': versions,
               'input': {'kind': 'prepared-snapshot', 'dsc_sha256': sha256(dsc),
                         'upstream_sha': entry['upstream_sha']}}
    if prepared['metadata'].get('remediation'):
        package['input']['remediation'] = prepared['metadata']['remediation']

    lock = {'schema_version': 1, 'target': target, 'catalog_entry': entry,
            'maintainer': {'name': 'Packaging Build Agent', 'email': 'packaging-agent@example.invalid'},
            'packages': [package]}
    return lock


def _record_build_result(build: LockedBuild, state: dict) -> None:
    """Record the overall build verdict and the first failed package stage."""
    failed = next((item for item in build.manifest['packages'] if item.get('result') == 'FAILED'), None)
    state.update(result=build.manifest['result'], generation=str(build.root))
    if failed:
        state['stage'] = failed.get('failed_stage', state['stage'])
        state['error'] = failed.get('error')


def _build_inputs(args: argparse.Namespace) -> tuple[dict, dict, dict, dict, dict, set[str]]:
    """Validate the frozen run and derive target and producer closure."""
    catalog = json.loads(args.catalog.read_text())
    snapshot_target = catalog_target(catalog)
    target = build_target(snapshot_target)
    identity = {'run_id': args.run_id, 'run_attempt': args.run_attempt}
    if catalog.get('ci') != identity:
        raise ValueError('Frozen catalog belongs to another pipeline run or attempt')

    entries = {p['source']: p for p in catalog['packages']}
    entry = entries[args.source]
    required = dependencies(entries, args.source)
    return snapshot_target, target, identity, entries, entry, required


def main() -> int:
    """Build one catalog source while publishing failures and blocked handoffs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--check-dependencies-only', action='store_true')
    parser.add_argument('--remediation-patch', type=Path)
    parser.add_argument('--prepared-source', type=Path,
                        help='Previously prepared immutable source artifact')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    state = {'source': args.source, 'result': 'PREPARING', 'stage': 'dependency-handoff'}
    try:
        return _execute_build(args, state)
    except Exception as error:
        state.update(result='BLOCKED' if state['stage'] == 'dependency-handoff' else 'FAILED', error=str(error))
        traceback.print_exc()
        return 1
    finally:
        if state['result'] != 'READY':
            (args.output / 'result.json').write_text(json.dumps(state, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
