"""Build one frozen catalog snapshot with verified same-run dependencies."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import traceback

from packagetest.artifacts import fields, sha256
from packagetest.contracts import (
    BuildTarget,
    PreparedSource,
    object_mapping,
    records,
    required_text,
    string_list,
    string_mapping,
    text_field,
)
from packagetest.prepared_plan import ActiveRelation, relation_groups
from packagetest.relations import BuildContext
from packagetest.handoff import build_repository, collect_producers, stamp_generation
from packagetest.locked import LockedBuild
from packagetest.snapshot_targets import build_target, catalog_target


@dataclass(frozen=True)
class BuildInputs:
    """Validated run, target, and producer selection for one catalog source."""

    source: str
    snapshot_target: dict[str, object]
    target: dict[str, object]
    identity: dict[str, str]
    entries: dict[str, dict[str, object]]
    required: frozenset[str]

    def __post_init__(self) -> None:
        """Reject invalid target identity or references outside the frozen catalog."""
        BuildTarget.from_mapping(self.target)
        if self.source not in self.entries:
            raise ValueError(f"Unknown catalog source: {self.source}")

        if self.required - self.entries.keys():
            raise ValueError(
                "Producer selection names sources outside the frozen catalog"
            )

    @property
    def entry(self) -> dict[str, object]:
        """Read the selected entry from the same catalog used for producer validation."""
        return self.entries[self.source]

    @property
    def context(self) -> BuildContext:
        """Derive dependency restriction evaluation from the validated target."""
        return BuildTarget.from_mapping(self.target).context


@dataclass(frozen=True)
class PreparedBuildSource:
    """Source descriptor and preparation evidence consumed by lock construction."""

    dsc: Path
    metadata: dict[str, object]
    reused: bool

    @classmethod
    def from_preparation(cls, value: dict[str, object]) -> PreparedBuildSource:
        """Validate a fresh preparation result at its existing JSON-shaped boundary."""
        dsc = required_text(value, "dsc")
        metadata = object_mapping(value["metadata"], "preparation metadata")
        return cls(Path(dsc), metadata, reused=False)


def dependencies(entries: dict[str, dict[str, object]], source: str) -> set[str]:
    """Collect transitive same-run producers from the frozen catalog graph."""
    result: set[str] = set()

    def visit(name: str) -> None:
        """Visit each producer once, including producers required by other producers."""
        for dep in string_list(
            entries[name].get("run_dependencies", []), "run_dependencies"
        ):
            if dep not in result:
                result.add(dep)
                visit(dep)

    visit(source)
    return result


def required_versions(
    source_fields: Mapping[str, str],
    producers: Mapping[str, Sequence[Mapping[str, object]]],
    context: BuildContext | None = None,
) -> dict[str, str]:
    """Select exact producer versions from the adapted source's dependencies."""
    names = _required_binary_names(source_fields, context)
    return {
        required_text(binary, "package"): required_text(binary, "version")
        for binaries in producers.values()
        for binary in binaries
        if binary["package"] in names
    }


def validate_candidate_dependencies(
    entry: Mapping[str, object],
    entries: Mapping[str, Mapping[str, object]],
    source_fields: Mapping[str, str],
    required: set[str],
    context: BuildContext,
) -> None:
    """A repair may not silently add edges to the frozen build graph.

    Existing archive bootstrap edges are intentional. A newly required
    selected producer needs a fresh plan, even when its archive version exists.
    """
    relationships = ", ".join(
        source_fields.get(key, "")
        for key in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch")
    )
    providers = {
        binary: name
        for name, candidate in entries.items()
        for binary in string_list(candidate.get("binaries", []), "binaries")
    }
    allowed = (
        required
        | set(
            string_list(
                entry.get("archive_bootstrap_dependencies", []),
                "archive_bootstrap_dependencies",
            )
        )
        | {required_text(entry, "source")}
    )
    original = ", ".join(
        string_mapping(
            entry.get("exact_build_depends", {}), "exact_build_depends"
        ).values()
    )
    original_groups = {
        group[0]["group"] for group in relation_groups(original, context)
    }
    _validate_new_groups(relationships, context, original_groups, providers, allowed)


def declared_binaries(source_fields: Mapping[str, str]) -> list[str]:
    """Use the prepared source package, rather than archive-era metadata."""
    binaries = sorted(
        name.strip()
        for name in source_fields.get("Binary", "").split(",")
        if name.strip()
    )
    if not binaries:
        raise ValueError("Prepared source .dsc declares no binary packages")
    return binaries


def _required_binary_names(
    source_fields: Mapping[str, str], context: BuildContext | None
) -> set[str]:
    """Resolve active source build-dependency binary names."""
    relationships = ", ".join(
        source_fields.get(key, "")
        for key in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch")
    )
    names = {
        item["package"]
        for group in relation_groups(relationships, context)
        for item in group
    }
    return names


def _validate_new_groups(
    relationships: str,
    context: BuildContext,
    original_groups: set[str],
    providers: Mapping[str, str],
    allowed: set[str],
) -> None:
    """Require newly introduced relation groups to stay within the frozen producer graph."""
    for alternatives in relation_groups(relationships, context):
        if alternatives[0]["group"] in original_groups:
            continue  # The frozen planner already decided how to satisfy this group.

        candidates = _group_providers(alternatives, providers)
        if candidates and not candidates & allowed:
            raise ValueError(
                "Source dependencies changed the frozen candidate graph; "
                f"replan required for {sorted(candidates)}"
            )


def _group_providers(
    alternatives: list[ActiveRelation], providers: Mapping[str, str]
) -> set[str]:
    """Find selected producers named by a dependency alternative group."""
    candidates = {
        providers[item["package"]]
        for item in alternatives
        if item["package"] in providers
    }
    return candidates


def _execute_build(args: argparse.Namespace, state: dict[str, object]) -> int:
    """Run frozen dependency handoff before any source preparation or build commands."""
    inputs = _build_inputs(args)
    locks = _dependency_locks(args.inputs, inputs.entries, inputs.required)
    if inputs.required - locks.keys():
        state["result"] = "BLOCKED"
        raise ValueError(
            f"Missing dependency builds: {sorted(inputs.required - locks.keys())}"
        )

    producers = collect_producers(
        args.inputs,
        locks,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        target=inputs.target,
    )
    if args.check_dependencies_only:
        state["result"] = "READY"
        return 0

    if producers:
        build_repository(producers, args.output / "dependency-repository")

    state["stage"] = "snapshot-source"
    prepared = _prepare_build_source(args, inputs)
    lock = _prepared_build_lock(
        inputs, prepared, producers, remediation=bool(args.remediation_patch)
    )
    state["stage"] = "binary-build"
    build = LockedBuild(
        lock,
        args.output,
        prepared_sources={args.source: prepared.dsc},
        external_artifacts=producers,
        timeout=5400,
    )
    result = build.run()
    stamp_generation(
        build.root / "generation-manifest.json",
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    _record_build_result(build, state)
    return result


def _dependency_locks(
    inputs: Path, entries: Mapping[str, Mapping[str, object]], required: frozenset[str]
) -> dict[str, dict[str, object]]:
    """Load one exact frozen catalog lock for each available producer."""
    locks: dict[str, dict[str, object]] = {}
    for path in inputs.rglob("build-lock.json"):
        lock = object_mapping(json.loads(path.read_text()), "dependency lock")
        entry = object_mapping(lock.get("catalog_entry", {}), "catalog_entry")
        name = text_field(entry, "source")
        if name in required:
            if name in locks:
                raise ValueError(f"Duplicate dependency lock: {name}")

            if lock["catalog_entry"] != entries[name]:
                raise ValueError(
                    f"Dependency input differs from frozen catalog: {name}"
                )

            locks[name] = lock

    return locks


def _prepare_build_source(
    args: argparse.Namespace, inputs: BuildInputs
) -> PreparedBuildSource:
    """Reuse verified prepared source or prepare fresh source for remediation."""
    if args.prepared_source:
        return _reuse_prepared_source(args, inputs)

    # Dependency-only preflight must stay usable before host build tools exist.
    from packagetest.nightly_source import prepare_source

    prepared = prepare_source(
        inputs.entry,
        args.output / "source-preparation",
        remediation_patch=args.remediation_patch,
        target=inputs.snapshot_target,
    )
    return PreparedBuildSource.from_preparation(prepared)


def _reuse_prepared_source(
    args: argparse.Namespace, inputs: BuildInputs
) -> PreparedBuildSource:
    """Verify frozen manifest identity and descriptor before reusing a prepared bundle."""
    if args.remediation_patch:
        raise ValueError("A remediation patch requires fresh source preparation")

    prepared_manifest = object_mapping(
        json.loads((args.prepared_source / "prepared-source.json").read_text()),
        "prepared source manifest",
    )
    _validate_prepared_manifest(prepared_manifest, args, inputs.entry, inputs.identity)
    prepared_identity = PreparedSource.from_mapping(prepared_manifest)
    dsc = prepared_identity.verified_path(args.prepared_source / "source")
    if not dsc.is_file() or sha256(dsc) != prepared_manifest["dsc_sha256"]:
        raise ValueError("Prepared source .dsc is missing or changed")

    resolution = args.prepared_source / "resolution.json"
    metadata = (
        object_mapping(json.loads(resolution.read_text()), "preparation metadata")
        if resolution.is_file()
        else {}
    )
    return PreparedBuildSource(dsc, metadata, reused=True)


def _validate_prepared_manifest(
    prepared_manifest: Mapping[str, object],
    args: argparse.Namespace,
    entry: Mapping[str, object],
    identity: Mapping[str, str],
) -> None:
    """Require both run identity and final-plan content hashes to match."""
    if (
        prepared_manifest.get("source") != args.source
        or prepared_manifest.get("ci") != identity
    ):
        raise ValueError("Prepared source identity differs from this build")

    expected = object_mapping(entry.get("prepared_source", {}), "prepared_source")
    if prepared_manifest.get("dsc_sha256") != expected.get(
        "dsc_sha256"
    ) or prepared_manifest.get("catalog_entry_sha256") != expected.get(
        "catalog_entry_sha256"
    ):
        raise ValueError("Prepared source differs from the final dependency plan")


def _prepared_build_lock(
    inputs: BuildInputs,
    prepared: PreparedBuildSource,
    producers: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    remediation: bool = False,
) -> dict[str, object]:
    """Build a lock using adapted source declarations and exact producer versions."""
    entry = inputs.entry
    dsc = prepared.dsc
    source_fields = fields(dsc)
    version = source_fields["Version"]
    if prepared.reused and version != entry.get("prepared_version"):
        raise ValueError(
            "Prepared source version differs from the final dependency plan"
        )

    # Reviewed packaging adaptations can add or tighten dependencies. Pin
    # the dependencies of the source we actually build, not its baseline.
    context = inputs.context
    if remediation:
        validate_candidate_dependencies(
            entry, inputs.entries, source_fields, set(inputs.required), context
        )

    versions = required_versions(source_fields, producers, context)
    expected_binaries = declared_binaries(source_fields)
    acquisition: dict[str, object] = {
        "kind": "prepared-snapshot",
        "dsc_sha256": sha256(dsc),
        "upstream_sha": entry["upstream_sha"],
    }
    package: dict[str, object] = {
        "source": inputs.source,
        "version": version,
        "expected_binaries": expected_binaries,
        "external_dependencies": sorted(producers),
        "required_build_versions": versions,
        "input": acquisition,
    }
    if prepared.metadata.get("remediation"):
        acquisition["remediation"] = prepared.metadata["remediation"]

    lock: dict[str, object] = {
        "schema_version": 1,
        "target": inputs.target,
        "catalog_entry": entry,
        "maintainer": {
            "name": "Packaging Build Agent",
            "email": "packaging-agent@example.invalid",
        },
        "packages": [package],
    }
    return lock


def _record_build_result(build: LockedBuild, state: dict[str, object]) -> None:
    """Record the overall build verdict and the first failed package stage."""
    failed = next(
        (item for item in build.manifest["packages"] if item.get("result") == "FAILED"),
        None,
    )
    state.update(result=build.manifest["result"], generation=str(build.root))
    if failed:
        state["stage"] = failed.get("failed_stage", state["stage"])
        state["error"] = failed.get("error")


def _build_inputs(args: argparse.Namespace) -> BuildInputs:
    """Validate the frozen run and derive target and producer closure."""
    catalog = object_mapping(json.loads(args.catalog.read_text()), "catalog")
    snapshot_target = catalog_target(catalog)
    target = build_target(snapshot_target)
    identity = {"run_id": args.run_id, "run_attempt": args.run_attempt}
    if catalog.get("ci") != identity:
        raise ValueError("Frozen catalog belongs to another pipeline run or attempt")

    entries = {
        required_text(p, "source"): p for p in records(catalog["packages"], "packages")
    }
    if args.source not in entries:
        raise ValueError(f"Unknown catalog source: {args.source}")

    required = frozenset(dependencies(entries, args.source))
    return BuildInputs(
        args.source, snapshot_target, target, identity, entries, required
    )


def main() -> int:
    """Build one catalog source while publishing failures and blocked handoffs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--check-dependencies-only", action="store_true")
    parser.add_argument("--remediation-patch", type=Path)
    parser.add_argument(
        "--prepared-source",
        type=Path,
        help="Previously prepared immutable source artifact",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {
        "source": args.source,
        "result": "PREPARING",
        "stage": "dependency-handoff",
    }
    try:
        return _execute_build(args, state)
    except Exception as error:
        state.update(
            result="BLOCKED" if state["stage"] == "dependency-handoff" else "FAILED",
            error=str(error),
        )
        traceback.print_exc()
        return 1
    finally:
        if state["result"] != "READY":
            (args.output / "result.json").write_text(json.dumps(state, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
