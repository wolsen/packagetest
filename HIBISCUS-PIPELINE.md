# OpenStack 2026.2 snapshot pipeline reference

Target: Ubuntu 26.04 (Resolute), amd64. The workflow builds candidate packages; it does not publish to Ubuntu Cloud Archive or execute regress-stack.

## Scope and inputs

`config/hibiscus-catalog.json` includes 170 cycle deliverables and 27 independently released build dependencies. Another 41 deliverables have no mapped Resolute source and remain explicit exclusions. Release metadata revisions and archive index checksums are recorded.

The plan resolves references once. Declared `stable/2026.2` branches take precedence. Each build checks out its frozen commit and applies checksum-pinned Ubuntu archive packaging. Snapshot versions contain commit date, distance from the base tag, and abbreviated SHA; commits at a tag use distance zero. Python sdists retain generated metadata. Swift explicitly opts out of a generated ChangeLog. Gnocchi uses its maintained GitHub repository and pinned setuptools-scm tools; the retired requestsexceptions dependency uses its last source commit, recorded explicitly in the catalog. Puppet sources use deterministic Git archives.

## Parallel builds and artifacts

The current graph has eight dependency levels, each allowing 20 concurrent jobs subject to runner availability. A dependency level is a topological layer: every package in it can build in parallel because any same-run candidate packages it needs are produced by earlier levels. GitHub job names show `Build dependency level N · SOURCE` and `Test dependency level N packages` so the layout explains both the ordering and the package being handled. Failures do not cancel independent jobs. Missing or failed required producers block consumers.

Cycles are recorded as archive bootstrap edges. Dependencies already available from an earlier dependency level use that run's candidates; only the remaining cycle edges use archive packages for the first build. A subsequent rebuild of cyclic components is required before claiming the complete set was built against candidate dependencies.

`config/hibiscus-candidate-dependencies.json` preserves reviewed dependencies that cannot use archive versions, even inside a cycle. For example, Manila Client requires the candidate OpenStack Client test fixtures, and oslo.service requires the candidate oslo.config serialization API. The reverse cycle edges can still bootstrap from the archive. An explicit pilot must include these mandatory candidates; incompatible mandatory cycles fail planning rather than silently falling back.

Consumers validate artifact run ID, attempt, target, frozen catalog entry, checksums, and Debian metadata. They reconstruct an APT repository and supply verified binaries to sbuild. Required versions are checked against buildinfo. Artifacts from older attempts are rejected; retry the whole workflow for dependency chains.

Planning incorporates checksum-verified packaging control replacements, so added build dependencies enter the graph. Exact version checks use all build-dependency fields from the generated source package, including architecture-independent dependencies.

## Tests and reports

Autopkgtests start after each dependency level while subsequent builds proceed. They use separate QEMU guests with 4 GiB RAM and two CPUs on KVM-capable GitHub runners. Built source packages and exact candidate binaries are tested. PASS, SUPERFICIAL, FAIL, SKIP, NO_TESTS, INFRA_ERROR, and BLOCKED remain distinct. Generated import checks marked superficial do not count as substantive coverage; neither do missing or skipped tests. SUPERFICIAL, SKIP, and NO_TESTS are recorded in JSON artifacts and the GitHub step summary but finish the job successfully. FAIL, INFRA_ERROR, and BLOCKED still fail the job.

The initial 197-source coverage audit found 108 sources with explicit tests, 44 with generated superficial imports only, and 45 with no detected package tests (26 Puppet modules, 18 Tempest plugins, and Aetos). Explicit tests range from imports and installation checks to unit suites and service checks; they are not equivalent to full cloud regression coverage.

Autopkgtest selects compatible binaries through each test’s dependency declarations. The runner does not install every binary simultaneously: Nova and Neutron contain conflicting alternatives. Exact candidate versions are pinned, and a dpkg hook checks the currently installed candidate subset after package operations, including variant switches performed by tests.

Artifacts `build-SOURCE` and `autopkgtest-SOURCE` contain tar bundles of results and logs. Small `status-*` artifacts feed `pipeline-summary`. Retention is 14 days.

The workflow runs on pushes to `codex/hibiscus-snapshot-pipeline` and manual dispatch. Empty `sources` selects the catalog; a comma-separated pilot list selects exactly those packages and records outside dependencies as archive bootstrap inputs. Its nightly schedule is 09:00 UTC (02:00 America/Phoenix). GitHub activates scheduled workflows only on the default branch, so recurring runs remain pending validation and default-branch integration; the feature branch is not currently scheduled.
