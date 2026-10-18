# Packaging executor architecture

Packaging tools build Debian packages. This application chooses pinned inputs,
checks the resulting artifacts, and preserves evidence at each boundary.

## Two entry paths

`packaging build --plan LOCK` executes a reviewed schema-v1 lock. It performs no
release discovery. `packaging plan` remains an advisory discovery interface.

The nightly path discovers a catalog, pins upstream and packaging commits,
prepares immutable sources, and derives a dependency graph from their metadata.
The GitHub workflows orchestrate this path through compatibility scripts that
call importable package modules.

```mermaid
flowchart LR
    A[Discover catalog and pin Git inputs] --> B[Prepare immutable sources]
    B --> C[Finalize dependency graph]
    C --> D[Build candidates by dependency level]
    D --> E[Autopkgtest in fresh guests]
    E --> F[Readiness and verified handoff]
    D --> G[Bounded remediation]
    E --> G
    G --> H[Fresh source and dependency checks]
    H --> D
    F --> I[Pipeline report and proposals]
```

Source-preparation failures have a separate remediation path before graph
finalization. During package remediation, a newly introduced selected build
producer outside the frozen graph requires replanning. Broader runtime/test
relationship replanning after repairs remains a limitation.

Typed boundaries separate artifact parsing from dependency decisions. Prepared
JSON is validated into named dictionary records before graph resolution. Those
records retain the published JSON layout; state owners such as
`ExactDependencyResolver` and `BuildInputs` organize the algorithms that consume
them. Opaque catalog and diagnostic extension fields remain `object` until a
consumer validates the fields it needs.

```mermaid
flowchart LR
    J[Prepared artifact JSON] --> V[Shape and identity validation]
    V --> M[Typed dependency metadata]
    M --> R[ExactDependencyResolver]
    A[Validated archive versions] --> R
    R --> P[Candidate edges and external decisions]
    V --> B[Checksum and inventory verification]
    B --> S[Verified source bundle]
```

## Ownership

| Area | Owner | Boundary |
| --- | --- | --- |
| Advisory release planning | `cli`, `planner.DependencyOrder`, `release_discovery.OpenStackReleaseResolver` | User configuration to advisory plan |
| Release metadata parsing | `SeriesStatusParser`, `DeliverableReleaseParser` | Ordered metadata fields to series and release identities |
| Catalog construction | `catalog.ArchiveSelection`, `CatalogBuilder` | Archive priority and release membership to transitive package closure |
| Packaging selection | `nightly_plan.PackagingResolution` | Ordered repository attempts to a pinned Git or published-source choice |
| Source discovery | `nightly_plan.SourceDiscovery` | Catalog roots to pinned upstream dependency closure |
| Requirement mapping | `upstream_dependencies.RequirementMapper` | One consumer’s source/binary provider evidence to candidate dependencies |
| Nightly scheduling | `nightly_plan.CatalogScheduler`, `final_plan`, `prepared_plan.DependencyPlan` | Selected catalog and prepared metadata to dependency waves |
| Plan publication | `nightly_plan.PlanPublisher` | Catalog and plan to hashed artifacts and workflow summaries |
| Prepared dependency resolution | `prepared_plan.ExactDependencyResolver` | Ordered candidate providers and archive evidence to exact dependency choices |
| Dependency syntax | `relations.BuildContext`, `DependencyRelation` | Target-aware restriction evaluation |
| JSON identity and metadata | `contracts.BuildTarget`, `PreparedSource`, `PreparedManifest` | Validated identities and typed dependency declarations |
| Nightly build inputs | `nightly_build.BuildInputs`, `PreparedBuildSource` | Validated run and source evidence to build-lock construction |
| Source orchestration | `nightly_source.SourcePreparer` | One workspace, command log, and preparation result |
| Packaging adaptations | `nightly_source.EntryPointEditor`, `BuildDependencyEditor` | Existing manifest ownership and archive proof to explicit edits and actions |
| Packaging checkout | `nightly_source.PackagingFetcher` | Pinned commit fetch with bounded host-specific retries |
| Quilt policy | `patches` | Evidence for applied patches and review suggestions |
| Quilt refresh | `failure_analysis.QuiltSection`, `QuiltPayload`, `QuiltSectionRefresh` | Validated sections, payload evidence, and isolated application probes |
| Locked execution | `locked.LockedBuild`, `commands.CommandRunner` | Lock to validated source/binary artifacts |
| Process capture | `commands.CommandCapture` | Process lifetime, deadline, output streams, and bounded log tails |
| Package publication | `publish-package-artifacts.PackageArtifacts` | Final readiness to compact reports, accepted handoff payloads, and failure diagnostics |
| Artifact handoff | `handoff.ProducerBundle`, `ProducerRepository`, `collect_producers` | Same-run producer identity, readiness, and byte verification |
| Remediation evidence | `remediation_evidence` | Ordered, bounded logs and source excerpts |
| Repair admission | `repair_policy` | Whole-file test, dependency, and check preservation at rendering and independent patch application boundaries |
| Local model investigation | `repair_agent.SourceInspector`, `PatchPlan`, `TextEdit` | Bounded read-only evidence and exact edits converted into reviewable Debian patches |
| Package remediation | `ProposalPlanner`, `CandidateExecutor`, `ArtifactPromoter` | Validated decisions, isolated build/test gates, and canonical artifact replacement |
| Retry coordination | `RemediationRun`, `RepairProgress`, `RemediationSession` | Current failure evidence, attempt records, unchanged-evidence retries, and budgets |
| Source remediation | `source_remediation.SourceRepairSession` | Cumulative general and quilt decisions, bounded retries, and prepared-source selection |
| Advisory failure analysis | `analyze-packaging-failure.AdvisoryAnalysis` | Prior response and validation evidence across two advisory attempts |
| Reporting | `pipeline_report`, `packaging_proposal`, `failure_summary` | Evidence to human-readable reports |

Objects own state and invariants. `LockedBuild`, `SourcePreparer`, and snapshot
discovery accept an injected `CommandExecutor` protocol for process-boundary
tests. Parsing, hashing, graph transformations, and
version calculations remain functions. No generic plugin framework or class
hierarchy is needed. JSON remains the interchange format; typed records are
used at identity and policy boundaries while preserving artifact schema
compatibility. The handoff module constructs repositories for verified candidate
packages.

`BuildInputs` names the selected catalog source, target, run identity, and
producer closure. `PreparedBuildSource` carries its descriptor, preparation
evidence, and reuse status. Keeping these records separate lets lock construction
check reused versions and repaired dependencies without depending on CLI options.

```mermaid
flowchart LR
    C[Frozen catalog and run identity] --> I[BuildInputs]
    S[Fresh or verified prepared source] --> P[PreparedBuildSource]
    I --> L[Build lock]
    P --> L
    D[Verified producer artifacts] --> L
    L --> B[LockedBuild]
```

`ExactDependencyResolver` captures its input manifests and archive evidence when
constructed. It resolves those inputs once and returns a separate copy of the
plan on each call. A consumer can annotate its result without changing another
consumer's dependency evidence.

`EntryPointEditor` owns the Debian manifest location and the baseline command
owners. Each edit returns a finding and an optional action; an ambiguous owner
remains a human decision. `BuildDependencyEditor` keeps source control text and
its dependency inventory together so a successful addition is recorded once.
The preparation module groups each policy with its private parsing and editing
helpers, allowing a maintainer to follow one responsibility within the file.

## Execution and evidence

The locked executor preflights the named schroot and tools, obtains pinned
source inputs, verifies the `.dsc` and its components, runs sbuild, checks actual
binary metadata and checksummed `.changes`/`.buildinfo`, then runs Lintian.
A successful command without the required artifacts is a failed build.

Command logs retain full stdout/stderr on disk and bounded tails in JSON.
`CommandResult` exposes the complete-output paths and provides `read_stdout()`
for metadata and history consumers. Its `stdout_tail` property reads only the
bounded diagnostic text. In-memory executors can supply complete stdout without
a file. `LockedBuild.command()` returns the tail unless `full_output=True` is
requested, keeping verbose build logs out of memory during ordinary builds.
Deadlines cover the entire subprocess lifetime, including time after its pipes
close. Build manifests use atomic replacement. Failures retain the stage,
packaging files, command details, and relevant Git evidence.

The standalone regression wrapper runs a fresh installation check after the
build. Nightly jobs use autopkgtest and a producer-readiness record. Build
success alone does not authorize downstream handoff; see the
[result contract](docs/contracts.md).

## Packaging changes and trust

Packaging Git commits and source checksums are pinned before execution.
Generated sdists use pinned build tools and normalized archive headers.
Fallback packaging sources and their reasons are recorded explicitly.
Only `PackagingResolutionError` permits trying another packaging origin: this
covers missing configured repositories or branches, failed Git queries, and
query timeouts. Unexpected internal errors propagate instead of becoming a
fallback decision, and packaging failures do not erase resolved upstream identity.

`SourcePreparer` keeps the preparation stages in one workspace: validate the
plan, select packaging, materialize upstream, generate the snapshot, preserve
supplementary archives, adapt packaging, and validate the generated source
package. Each stage records evidence in the same resolution report, so a later
failure retains earlier decisions. Entry-point, dependency, and autopkgtest
adaptations have separate policies and contribute to the reviewable proposal.

Quilt identifier overlap is advisory. It never comments out a patch in `series`.
A failed active patch continues to fail source validation. Existing reverse-apply
and matching upstream Change-Id policies are separate mechanisms, with their
own recorded evidence; their assumptions are not semantic equivalence proofs.

Model output is untrusted. Deterministic validation restricts repair paths and
rejects known test-bypass patterns, but text checks are not a security sandbox.
Run source generation, packaging rules, builds, and tests in disposable builders
without publication credentials. A proposed repair is promoted only after its
fresh build and accepted autopkgtest outcome; all attempts retain evidence.
Human-review flags describe proposal review, not a guarantee that review has
already happened.

## Reproducibility and scope

Equal locks do not promise byte-identical binaries. Archive dependencies are
current at build time and recorded, rather than served from immutable snapshots.
The locked executor does not independently authenticate archive `.dsc`
signatures. Same-run handoff verifies identity and bytes but is not a signed
public archive. No automatic signing, Launchpad upload, or regress-stack gate is
connected. Current operational targets and schedules belong in the
[nightly reference](HIBISCUS-PIPELINE.md), not duplicated here.

## Artifact roles

Prepared source bundles cross the preparation-to-build boundary. Accepted
producer bundles carry only the identities and source/binary files needed for
independent handoff verification. Compact package reports carry both phase
verdicts, evolution, proposals, and accepted repairs to the aggregate renderer.
Full build, test, and AI diagnostics share one failure archive; blocked consumers
need only their report. Keeping these roles separate bounds artifact count and
keeps summary downloads independent of large binary and test payloads. The
[publication contract](docs/contracts.md#package-artifact-publication) defines
conditions, layout, and the per-run artifact bound.
