"""Prepare one immutable snapshot source and export its exact Debian metadata."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback

from packagetest.artifacts import checksum_entries, fields, sha256, verify_source
from packagetest.catalog import paragraphs
from packagetest.contracts import (
    PreparedSource,
    PreparedManifest,
    ControlMetadata,
    BinaryMetadata,
    AutopkgtestMetadata,
    object_mapping,
    records,
    text_field,
    prepared_manifest,
)
from packagetest.nightly_source import prepare_source
from packagetest.prepared_plan import verify_prepared_bundle
from packagetest.snapshot_targets import catalog_target


def digest(value: object) -> str:
    """Hash a stable JSON representation for the frozen catalog identity."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _binary_metadata(records: list[dict[str, str]]) -> list[BinaryMetadata]:
    """Read declared binary packages and their dependency restrictions."""
    binaries: list[BinaryMetadata] = []
    for record in records:
        if "Package" not in record:
            continue

        binaries.append(
            {
                "package": record["Package"],
                "architecture": record.get("Architecture", "any"),
                "depends": record.get("Depends", ""),
                "pre_depends": record.get("Pre-Depends", ""),
                "recommends": record.get("Recommends", ""),
                "build_profiles": record.get("Build-Profiles", ""),
            }
        )

    return binaries


def _test_metadata(tests_control: Path) -> list[AutopkgtestMetadata]:
    """Read optional autopkgtest definitions without inventing test dependencies."""
    tests: list[AutopkgtestMetadata] = []
    if tests_control.is_file():
        for record in paragraphs(tests_control.read_text()):
            tests.append(
                {
                    "tests": record.get("Tests", record.get("Test-Command", "unnamed")),
                    "depends": record.get("Depends", ""),
                    "restrictions": record.get("Restrictions", ""),
                    "features": record.get("Features", ""),
                }
            )

    return tests


def control_metadata(tree: Path) -> ControlMetadata:
    """Read exact build, binary, and test dependencies from the prepared tree."""
    records = paragraphs((tree / "debian/control").read_text())
    if not records or "Source" not in records[0]:
        raise ValueError("Prepared debian/control has no source paragraph")

    source = records[0]
    binaries = _binary_metadata(records[1:])
    tests = _test_metadata(tree / "debian/tests/control")
    return {
        "build_depends": {
            key: source.get(key, "")
            for key in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch")
        },
        "binary_packages": binaries,
        "autopkgtests": tests,
    }


def _declared_binaries(
    source_fields: dict[str, str], metadata: ControlMetadata
) -> list[str]:
    """Require nonempty binary declarations that agree across source metadata."""
    declared = sorted(
        name.strip()
        for name in source_fields.get("Binary", "").split(",")
        if name.strip()
    )
    _validate_binary_declarations(declared, metadata)

    return declared


def _validate_binary_declarations(
    declared: list[str], metadata: ControlMetadata
) -> None:
    """Reject missing or contradictory binary identities before copying artifacts."""
    control_declared = sorted(item["package"] for item in metadata["binary_packages"])
    if declared != control_declared:
        raise ValueError(
            f".dsc binaries differ from prepared debian/control: "
            f"{declared} != {control_declared}"
        )

    if not declared:
        raise ValueError("Prepared source declares no binary packages")


def _copy_source_bundle(
    dsc: Path,
    source_fields: dict[str, str],
    prepared: Mapping[str, object],
    output: Path,
) -> list[Path]:
    """Copy the verified artifacts and associated provenance into a new bundle."""
    output.mkdir(parents=True, exist_ok=False)
    source_dir = output / "source"
    source_dir.mkdir()
    files = [
        dsc,
        *(dsc.parent / name for _, _, name in checksum_entries(source_fields)),
    ]
    for path in files:
        shutil.copy2(path, source_dir / path.name)

    proposal = dsc.parent / "packaging-proposal"
    if proposal.is_dir():
        shutil.copytree(proposal, output / "packaging-proposal")

    resolution = Path(text_field(prepared, "lock"))
    shutil.copy2(resolution, output / "resolution.json")

    return files


def export_prepared(
    prepared: Mapping[str, object],
    entry: Mapping[str, object],
    catalog: Mapping[str, object],
    output: Path,
) -> PreparedManifest:
    """Verify and export an immutable source bundle with its planning metadata."""
    dsc = Path(text_field(prepared, "dsc"))
    tree = Path(text_field(prepared, "tree"))
    source_fields = verify_source(
        dsc, text_field(entry, "source"), fields(dsc)["Version"]
    )
    metadata = control_metadata(tree)
    declared = _declared_binaries(source_fields, metadata)

    files = [
        dsc,
        *(dsc.parent / name for _, _, name in checksum_entries(source_fields)),
    ]

    manifest = prepared_manifest(
        {
            "schema_version": 1,
            "source": entry["source"],
            "version": source_fields["Version"],
            "binaries": declared,
            **metadata,
            "testsuite": source_fields.get("Testsuite", ""),
            "testsuite_triggers": source_fields.get("Testsuite-Triggers", ""),
            "dsc": dsc.name,
            "dsc_sha256": sha256(dsc),
            "source_artifacts": [
                {"file": path.name, "sha256": sha256(path)} for path in files
            ],
            "catalog_entry_sha256": digest(entry),
            "pins": {
                "upstream_sha": entry["upstream_sha"],
                "packaging_sha": entry.get("packaging_sha"),
            },
            "packaging_source": object_mapping(
                prepared["metadata"], "preparation metadata"
            ).get("packaging_source"),
            "ci": catalog["ci"],
        }
    )
    _copy_source_bundle(dsc, source_fields, prepared, output)
    (output / "prepared-source.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _copy_failure_evidence(work: Path, output: Path) -> None:
    """Retain available resolution and command logs after source preparation fails."""
    if work.exists():
        evidence = output / "failure-evidence"
        evidence.mkdir(exist_ok=True)
        for name in ("resolution.json",):
            path = work / name
            if path.is_file():
                shutil.copy2(path, evidence / name)

        logs = work / "logs"
        if logs.is_dir():
            shutil.copytree(logs, evidence / "logs", dirs_exist_ok=True)


def _validate_restore_identity(
    manifest: PreparedManifest,
    entry: Mapping[str, object],
    ci: Mapping[str, str] | None,
) -> None:
    """Bind the repair baseline to the same source, run, and finalized source hashes."""
    if manifest.get("source") != entry["source"] or manifest.get("ci") != ci:
        raise ValueError(
            "Repair source bundle belongs to another source or pipeline run"
        )

    expected = object_mapping(entry["prepared_source"], "prepared_source")
    for key in ("dsc_sha256", "catalog_entry_sha256"):
        if manifest.get(key) != expected.get(key):
            raise ValueError(
                "Repair source bundle differs from the frozen dependency plan"
            )


def restore_prepared_tree(
    bundle: Path, entry: Mapping[str, object], ci: Mapping[str, str] | None
) -> Path:
    """Extract a verified frozen source bundle as the baseline for packaging repairs."""
    path = bundle / "prepared-source.json"
    manifest = prepared_manifest(json.loads(path.read_text()))
    identity = PreparedSource.from_mapping(manifest)
    _validate_restore_identity(manifest, entry, ci)
    verify_prepared_bundle(path, identity, manifest)
    dsc = identity.verified_path(bundle / "source")
    destination = bundle / "remediation-source"
    with tempfile.TemporaryDirectory(prefix=".remediation-", dir=bundle) as temporary:
        tree = Path(temporary) / "source"
        # Repairs target packaging against the original upstream files. Applying
        # quilt here would hide the context needed to refresh a failing patch.
        subprocess.run(
            ["dpkg-source", "--skip-patches", "-x", str(dsc), str(tree)],
            text=True,
            capture_output=True,
            check=True,
            timeout=180,
        )
        if fields(tree / "debian/control").get("Source") != identity.source:
            raise ValueError(
                "Restored repair tree has the wrong Debian source identity"
            )

        tree.rename(destination)

    return destination


def main() -> int:
    """Prepare the requested frozen source and publish either metadata or failure evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--remediation-patch", type=Path)
    args = parser.parse_args()
    catalog = object_mapping(json.loads(args.catalog.read_text()), "catalog")
    target = catalog_target(catalog)
    identity = {"run_id": str(args.run_id), "run_attempt": str(args.run_attempt)}
    if catalog.get("ci") != identity:
        raise ValueError("Frozen catalog belongs to another pipeline run or attempt")

    entries = {
        text_field(entry, "source"): entry
        for entry in records(catalog.get("packages"), "packages")
    }
    if args.source not in entries:
        raise ValueError(f"Unknown frozen source: {args.source}")

    try:
        prepared = prepare_source(
            entries[args.source],
            args.work,
            remediation_patch=args.remediation_patch,
            target=target,
        )
        manifest = export_prepared(prepared, entries[args.source], catalog, args.output)
        print(
            json.dumps(
                {
                    "result": "PREPARED",
                    "source": args.source,
                    "version": manifest["version"],
                    "binaries": manifest["binaries"],
                }
            )
        )
        return 0
    except Exception as exc:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "result.json").write_text(
            json.dumps(
                {
                    "source": args.source,
                    "result": "FAILED",
                    "stage": "source-preparation",
                    "error": str(exc),
                    "ci": identity,
                },
                indent=2,
            )
            + "\n"
        )
        _copy_failure_evidence(args.work, args.output)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
