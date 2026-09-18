# DESIGN

## Goals

- Automate Ubuntu OpenStack package update workflows using standard Debian/Ubuntu tooling.
- Keep all important external commands visible and auditable.
- Build a dependency-aware scheduler that understands package relationships.
- Produce reproducible generation metadata and rich failure artifacts.
- Maintain a strict review boundary for AI-proposed fixes.

## Non-goals

- Replacing `gbp`, `quilt`, `dpkg-buildpackage`, or `sbuild`.
- Launchpad uploads, PPA publication, or long-lived key handling.
- Hiding packaging mechanics behind custom abstractions.

## Reference-guide alignment and intentional differences

The provided manual packaging guide is a strong workflow reference, but this repository intentionally differs for GitHub-hosted CI:

- **No Launchpad upload path** (`dput`, credentials, merge proposals) in execution steps.
- **Ephemeral signing only** for local repository metadata where required.
- **No persistent machine assumptions** (all state is artifact-backed per generation).
- **Scheduler is Python source of truth**, not workflow YAML logic.

## Architecture

- `packaging plan`: resolve package definitions and dependencies into a DAG-backed plan.
- `packaging build`: orchestrate package operations for ready nodes and record command telemetry.
- `packaging status`: expose persisted generation state/manifest data.

Key modules:

- `config.py` — declarative package definitions.
- `planner.py` — graph construction and topological ordering.
- `scheduler.py` — state transitions and ready/blocked handling.
- `commands.py` — command execution and telemetry capture.
- `repository.py` — APT repository command generation abstraction.
- `manifest.py` — generation provenance serialization.
- `failures.py` — failure bundle materialization.

## Packaging lifecycle (per source package)

1. Obtain Ubuntu packaging git repository.
2. Obtain upstream source/tag/snapshot.
3. Import upstream source (`gbp import-orig`, pristine-tar aware).
4. Import patch queue (`gbp pq import`), detect refresh/apply failure.
5. Update packaging metadata/changelog.
6. Build source package.
7. Build binaries with `sbuild`.
8. Publish outputs into generation-scoped APT repository artifacts.

## Dependency DAG model

The planner models source-level build dependencies and scheduler states:

- `WAITING_FOR_DEPENDENCY`
- `BUILDING`
- `BUILD_FAILED`
- `BUILD_SUCCEEDED`
- `PUBLISHED`
- `BLOCKED_BY_FAILED_DEPENDENCY`

Dependency deadlocks are represented as scheduling states, not compiler/build failures.

## GitHub Actions execution model

- Use GitHub Actions as an execution substrate only.
- Plan artifacts are produced once and consumed by downstream jobs.
- Dependency layers are represented by job dependencies; ready packages fan out as matrix work.
- APT repository state is generation-scoped and artifact-backed.

## Artifact and repository model

Each generation creates isolated artifacts:

- command logs (`commands.jsonl`)
- package build outputs
- APT metadata (`Packages`, `Release`, `InRelease`, `Release.gpg`)
- generation manifest
- failure bundles

Repository abstraction is intentionally replaceable so future persistent APT servers can be introduced without redesigning packaging orchestration.

## Build generations, provenance, reproducibility

A generation is identified by `generation_id`. Manifest entries capture:

- release target and Ubuntu series
- package source/upstream/packaging refs
- generated versions and hashes
- dependency versions used
- runner environment and timestamps
- terminal build state

Generations are isolated to avoid cross-run package contamination.

## Failure handling

On real packaging/build failure, collect and persist:

- failing command and exit details
- environment/cwd and full command logs
- package metadata files (`debian/control`, `debian/rules`, `debian/changelog`, patch series)
- source/packaging revision context

Failure classes include:

- `PATCH_APPLY_FAILURE`
- `MISSING_BUILD_DEPENDENCY`
- `DEPENDENCY_VERSION_CONFLICT`
- `COMPILATION_FAILURE`
- `UNIT_TEST_FAILURE`
- `PACKAGING_POLICY_FAILURE`
- `SOURCE_GENERATION_FAILURE`
- `UNKNOWN`

## AI remediation boundary

The AI stage is optional and strictly review-bound:

- Input: failure bundle
- Output artifacts only: `analysis.md`, `proposed-fix.patch`
- No automatic patch application
- No automatic resume/continue after AI output

## Security model

- No Launchpad publication capability in this repo/workflow.
- Do not expose unnecessary GitHub tokens into build environments.
- Do not execute AI-generated shell commands automatically.
- Separate upstream verification keys from local artifact-signing keys.
- Destroy ephemeral signing key material at end of run.

## Future shared-repository support

The current artifact-backed repository mechanism is intentionally abstract. Later backends may include a dedicated shared APT server with identical package-consumer semantics from the `sbuild` perspective.
