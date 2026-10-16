# OpenStack 2026.2 snapshot pipeline reference

Targets: Ubuntu 26.10 (Stonking) development and the Hibiscus Ubuntu Cloud
Archive for Ubuntu 26.04 LTS (Resolute), both amd64. Separate workflows build
candidate packages; it does not publish to Ubuntu or the Ubuntu Cloud Archive,
and it does not execute regress-stack.

## Release and comparison policy

The target pair is derived from the OpenStack release number. OpenStack
`YYYY.1` maps to Ubuntu `YY.04`, and `YYYY.2` maps to Ubuntu `YY.10`. An
even-year April Ubuntu release is an LTS. Its Cloud Archive receives the next
four OpenStack releases: the `.2` release from that year, both releases from
the following year, and the next even year's `.1` release. Consequently,
Hibiscus `2026.2` maps to Ubuntu 26.10 and the Resolute 26.04 UCA. Indri
`2027.1` will map to Ubuntu 27.04 and the same Resolute UCA.

Each target downloads its own live source indexes at the beginning of the run.
Source-evolution commit lists start at the upstream version represented by the
package selected from that target. Stonking therefore compares with its
published Hibiscus package. The Resolute target overlays
`resolute-updates/hibiscus` on the base Resolute indexes and compares with the
latest version actually published in that UCA pocket. If a UCA package has not
yet advanced from its seeded version, the longer commit delta remains visible
rather than being mislabeled as current Hibiscus packaging.

## Scope and inputs

At the start of each run, `scripts/generate-nightly-catalog.py` joins current
OpenStack release metadata with the selected Ubuntu target's live release,
updates, and security source indexes. UCA targets add their signed Cloud Archive
source index at higher version precedence. The catalog records explicit
exclusions, checksums the input indexes, and
preflights every selected Ubuntu source file. The resulting catalog is frozen
inside the `nightly-plan` artifact rather than committed to the repository.
The same planning pass pins source-package Git commits, preferring Ubuntu
OpenStack Launchpad trees and falling back to the archive `Vcs-Git` repository.
Build jobs fetch those exact commits for their `debian/` packaging.

The plan resolves references once. Declared `stable/2026.2` branches take precedence. Each build checks out its frozen upstream and packaging commits, then applies the pinned packaging `debian/` tree to the snapshot. Snapshot versions contain commit date, distance from the base tag, and abbreviated SHA; commits at a tag use distance zero. Python sdists retain generated metadata. Swift explicitly opts out of a generated ChangeLog. Gnocchi uses its maintained GitHub repository and pinned setuptools-scm tools; the retired requestsexceptions dependency uses its last source commit, recorded explicitly in the catalog. Puppet sources use deterministic Git archives.

## Parallel builds and artifacts

The planner derives dependency levels from each run’s prepared sources; each level allows up to 20 concurrent jobs subject to runner availability. A dependency level is a topological layer: every package in it can build in parallel because any same-run candidate packages it needs are produced by earlier levels. The planning job publishes the complete level-by-level package list, dependency policy, and source-resolution status in its GitHub step summary and in `nightly-plan/summary.md`. It also emits a dynamic matrix containing only nonempty levels. GitHub runs that outer matrix one level at a time; a reusable workflow expands the packages within the current level into parallel `Build SOURCE` jobs. This avoids predeclared empty job groups while preserving candidate artifact handoff between levels. Failures do not cancel independent jobs. Missing or failed required producers block consumers.

Cycles are recorded as archive bootstrap edges. Dependencies already available from an earlier dependency level use that run's candidates; only the remaining cycle edges use archive packages for the first build. A subsequent rebuild of cyclic components is required before claiming the complete set was built against candidate dependencies.

`src/packagetest/data/hibiscus-candidate-dependencies.json` preserves reviewed dependencies that cannot use archive versions, even inside a cycle. For example, Manila Client requires the candidate OpenStack Client test fixtures, and oslo.service requires the candidate oslo.config serialization API. The reverse cycle edges can still bootstrap from the archive. An explicit pilot must include these mandatory candidates; incompatible mandatory cycles fail planning rather than silently falling back.

Consumers validate artifact run ID, attempt, target, frozen catalog entry, checksums, and Debian metadata. They reconstruct an APT repository and supply verified binaries to sbuild. Required versions are checked against buildinfo. Artifacts from older attempts are rejected; retry the whole workflow for dependency chains.

Planning incorporates checksum-verified packaging control replacements, so added build dependencies enter the graph. Exact version checks use all build-dependency fields from the generated source package, including architecture-independent dependencies.

## Tests and reports

Each `Build SOURCE` job runs autopkgtest immediately after producing and preserving its candidate artifacts. The next dependency level waits for both operations, so a package is complete only after its build and installed-package test have run. Tests use separate QEMU guests with 4 GiB RAM and two CPUs on KVM-capable GitHub runners. Built source packages and exact candidate binaries are tested. PASS, SUPERFICIAL, FAIL, SKIP, NO_TESTS, INFRA_ERROR, and BLOCKED remain distinct. Generated import checks marked superficial do not count as substantive coverage; neither do missing or skipped tests. SUPERFICIAL, SKIP, and NO_TESTS are recorded in JSON artifacts and the GitHub step summary but finish the package job successfully. FAIL, INFRA_ERROR, and BLOCKED fail it.

Autopkgtest selects compatible binaries through each test’s dependency declarations. The runner does not install every binary simultaneously: Nova and Neutron contain conflicting alternatives. Exact candidate versions are pinned, and a dpkg hook checks the currently installed candidate subset after package operations, including variant switches performed by tests.

Artifacts `build-TARGET-SOURCE` and `autopkgtest-TARGET-SOURCE` contain tar
bundles of results and logs. Target-qualified `status-*` artifacts feed one
static pipeline report per target. Retention is 14 days.

## Experimental local AI failure analysis

The pinned local model is prepared and smoke-tested after planning and before the first package level starts. Each package job first finishes its build and autopkgtest processes. When either gate fails, that same job restores the model cache and makes bounded Debian-packaging repair attempts. The `llama-cli` process exits before each rebuild starts, so model inference and sbuild or autopkgtest do not occupy memory at the same time on a runner. Every applicable patch is rebuilt from a fresh prepared source and rerun through autopkgtest. Only an attempt that passes both gates replaces the job's canonical build and test artifacts, so later dependency levels consume the repaired packages. Prompts, raw model output, proposed patches, validation results, rebuild logs, and test logs are uploaded as `ai-remediation-TARGET-SOURCE`.

Inference stays on the GitHub runner. The experiment uses the CPU build of llama.cpp `b10964` and the official Apache-2.0 Qwen2.5-Coder-7B-Instruct Q4_K_M model. Both downloads have pinned revisions and SHA256 values. The production pipeline verifies the cached files and performs a smoke inference; a separate focused model-contract workflow exercises response parsing and patch validation whenever the repair implementation changes. Prompts are bounded to selected error logs and the failed package's prepared source and Debian packaging; the model does not receive GitHub credentials.

The package-remediation defaults allow eight model calls, four rebuilds, eight validated repairs, and 45 minutes total; CLI/workflow arguments may lower these budgets. Source-preparation remediation has a separate budget. Later attempts receive the preceding validation or build/test result and retain earlier accepted changes when a rebuild exposes another missing dependency. Model output is treated as untrusted data: free-form commands are never executed, changes are restricted to `debian/`, `debian/changelog` and test-bypass changes are rejected, and a proposed diff must apply cleanly to the prepared source. An accepted diff is then applied during a fresh source preparation and must complete the real sbuild and autopkgtest gates.

Every failed `Build SOURCE` job writes its repair table to the GitHub job summary. Its downloadable `ai-remediation-TARGET-SOURCE` artifact contains prompts, raw model responses, proposed patches, validation JSON, rebuild and autopkgtest logs, and a machine-readable result. A successful repair replaces the failed candidate before the `build-TARGET-SOURCE` and `autopkgtest-TARGET-SOURCE` artifacts are uploaded. An unresolved package retains its original failure evidence and fails its package job.

The targets cannot be combined in one GitHub Actions run. Ubuntu Stonking runs
nightly at 09:00 UTC and Resolute UCA runs at 10:00 UTC, which keeps each run's
job graph small enough to inspect. Both workflows also run on relevant pushes
to `main` and by manual dispatch. Pushes select a representative dependency
closure. An empty manual `sources` value selects the complete catalog; a
comma-separated list selects requested roots whose candidate dependency closure
is resolved during planning. Archive bootstrap decisions are recorded explicitly.

## Python implementation and contracts

The script paths are CLI adapters; reusable implementations live
in the `packagetest` package. Target and prepared-source identities are validated
at JSON boundaries. See [architecture](DESIGN.md) and
[pipeline contracts](docs/contracts.md) for ownership and compatibility rules.
Dependency restrictions are evaluated for the target architecture and active
build profiles. Quilt identifier overlap only suggests review and does not
disable a failing patch. Repairs that add an unplanned selected build producer
require a new dependency plan.
