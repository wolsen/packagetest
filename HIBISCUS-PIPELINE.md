# OpenStack 2026.2 snapshot pipeline reference

Target: Ubuntu 26.04 (Resolute), amd64. The workflow builds candidate packages; it does not publish to Ubuntu Cloud Archive or execute regress-stack.

## Scope and inputs

`config/hibiscus-catalog.json` includes 170 cycle deliverables and 27 independently released build dependencies. Another 41 deliverables have no mapped Resolute source and remain explicit exclusions. Release metadata revisions and archive index checksums are recorded.

The plan resolves references once. Declared `stable/2026.2` branches take precedence. Each build checks out its frozen commit and applies checksum-pinned Ubuntu archive packaging. Snapshot versions contain commit date, distance from the base tag, and abbreviated SHA; commits at a tag use distance zero. Python sdists retain generated metadata. Swift explicitly opts out of a generated ChangeLog. Gnocchi uses its maintained GitHub repository and pinned setuptools-scm tools; the retired requestsexceptions dependency uses its last source commit, recorded explicitly in the catalog. Puppet sources use deterministic Git archives.

## Parallel builds and artifacts

The current graph has eight dependency levels, each allowing 20 concurrent jobs subject to runner availability. A dependency level is a topological layer: every package in it can build in parallel because any same-run candidate packages it needs are produced by earlier levels. The planning job publishes the complete level-by-level package list, dependency policy, and source-resolution status in its GitHub step summary and in `nightly-plan/summary.md`. GitHub groups each matrix as `build_dependency_level_N`; each child remains `Build SOURCE` and runs both the package build and its autopkgtest. Failures do not cancel independent jobs. Missing or failed required producers block consumers.

Cycles are recorded as archive bootstrap edges. Dependencies already available from an earlier dependency level use that run's candidates; only the remaining cycle edges use archive packages for the first build. A subsequent rebuild of cyclic components is required before claiming the complete set was built against candidate dependencies.

`config/hibiscus-candidate-dependencies.json` preserves reviewed dependencies that cannot use archive versions, even inside a cycle. For example, Manila Client requires the candidate OpenStack Client test fixtures, and oslo.service requires the candidate oslo.config serialization API. The reverse cycle edges can still bootstrap from the archive. An explicit pilot must include these mandatory candidates; incompatible mandatory cycles fail planning rather than silently falling back.

Consumers validate artifact run ID, attempt, target, frozen catalog entry, checksums, and Debian metadata. They reconstruct an APT repository and supply verified binaries to sbuild. Required versions are checked against buildinfo. Artifacts from older attempts are rejected; retry the whole workflow for dependency chains.

Planning incorporates checksum-verified packaging control replacements, so added build dependencies enter the graph. Exact version checks use all build-dependency fields from the generated source package, including architecture-independent dependencies.

## Tests and reports

Each `Build SOURCE` job runs autopkgtest immediately after producing and preserving its candidate artifacts. The next dependency level waits for both operations, so a package is complete only after its build and installed-package test have run. Tests use separate QEMU guests with 4 GiB RAM and two CPUs on KVM-capable GitHub runners. Built source packages and exact candidate binaries are tested. PASS, SUPERFICIAL, FAIL, SKIP, NO_TESTS, INFRA_ERROR, and BLOCKED remain distinct. Generated import checks marked superficial do not count as substantive coverage; neither do missing or skipped tests. SUPERFICIAL, SKIP, and NO_TESTS are recorded in JSON artifacts and the GitHub step summary but finish the package job successfully. FAIL, INFRA_ERROR, and BLOCKED fail it.

The initial 197-source coverage audit found 108 sources with explicit tests, 44 with generated superficial imports only, and 45 with no detected package tests (26 Puppet modules, 18 Tempest plugins, and Aetos). Explicit tests range from imports and installation checks to unit suites and service checks; they are not equivalent to full cloud regression coverage.

Autopkgtest selects compatible binaries through each test’s dependency declarations. The runner does not install every binary simultaneously: Nova and Neutron contain conflicting alternatives. Exact candidate versions are pinned, and a dpkg hook checks the currently installed candidate subset after package operations, including variant switches performed by tests.

Artifacts `build-SOURCE` and `autopkgtest-SOURCE` contain tar bundles of results and logs. Small `status-*` artifacts feed `pipeline-summary`. Retention is 14 days.

## Experimental local AI failure analysis

The pinned local model is prepared and smoke-tested after planning and before the first package level starts. Each package job first finishes its build and autopkgtest processes. When either gate fails, that same job restores the model cache and makes up to three bounded Debian-packaging patch attempts. The `llama-cli` process exits before each rebuild starts, so model inference and sbuild or autopkgtest do not occupy memory at the same time on a runner. Every applicable patch is rebuilt from a fresh prepared source and rerun through autopkgtest. Only an attempt that passes both gates replaces the job's canonical build and test artifacts, so later dependency levels consume the repaired packages. Prompts, raw model output, proposed patches, validation results, rebuild logs, and test logs are uploaded as `ai-remediation-<source>`.

Inference stays on the GitHub runner. The experiment uses the CPU build of llama.cpp `b10964` and the official Apache-2.0 Qwen2.5-Coder-7B-Instruct Q4_K_M model. Both downloads have pinned revisions and SHA256 values, and real contract inferences run before the focused canary package begins. Prompts are bounded to selected error logs and the failed package's prepared source and Debian packaging; the model does not receive GitHub credentials.

Each repair makes up to three attempts. Later attempts receive the preceding validation or build/test result and retain earlier accepted changes when a rebuild exposes another missing dependency. Model output is treated as untrusted data: free-form commands are never executed, changes are restricted to `debian/`, `debian/changelog` and test-bypass changes are rejected, and a proposed diff must apply cleanly to the prepared source. An accepted diff is then applied during a fresh source preparation and must complete the real sbuild and autopkgtest gates.

Every failed `Build SOURCE` job writes its repair table to the GitHub job summary. Its downloadable `ai-remediation-SOURCE` artifact contains both prompts, raw model responses, proposed patches, validation JSON, rebuild and autopkgtest logs, and a machine-readable result. A successful repair replaces the failed candidate before the `build-SOURCE` and `autopkgtest-SOURCE` artifacts are uploaded. An unresolved package retains its original failure evidence and fails its package job.

An initial local sanity check used the larger Qwen3.5 4.7B Q4_K_M model on the retained Heat failure. It found the `heatclient.v1` and `magnumclient.v1` import symptoms, but it did not identify the confirmed producer-package discovery defect or produce a complete patch in either attempt. Inference took 133–273 seconds per attempt on the local CPU. The in-job experiment now measures the smaller coding-specific model by the outcome that matters: whether its patch produces a package that rebuilds and passes the installed-package gate.

The workflow runs on pushes to `codex/hibiscus-snapshot-pipeline` and manual dispatch. Empty `sources` selects the catalog; a comma-separated pilot list selects exactly those packages and records outside dependencies as archive bootstrap inputs. Its nightly schedule is 09:00 UTC (02:00 America/Phoenix). GitHub activates scheduled workflows only on the default branch, so recurring runs remain pending validation and default-branch integration; the feature branch is not currently scheduled.
