# OpenStack 2026.2 snapshot pipeline reference

Target: Ubuntu 26.04 (Resolute), amd64. The workflow builds candidate packages; it does not publish to Ubuntu Cloud Archive or execute regress-stack.

## Scope and inputs

`config/hibiscus-catalog.json` includes 172 cycle deliverables and 26 independently released build dependencies. Another 39 deliverables have no mapped Resolute source and remain explicit exclusions. Release metadata revisions and archive index checksums are recorded.

The plan resolves references once. Declared `stable/2026.2` branches take precedence. Each build checks out its frozen commit and applies checksum-pinned Ubuntu archive packaging. Snapshot versions contain commit date, distance from the base tag, and abbreviated SHA; commits at a tag use distance zero. Python sdists retain generated metadata. Puppet sources use deterministic Git archives.

## Parallel builds and artifacts

The initial graph has eight waves, each allowing 20 concurrent jobs subject to runner availability. Failures do not cancel independent jobs. Missing or failed required producers block consumers.

Cycles are recorded as archive bootstrap edges. Those dependencies use archive packages for the first build. A subsequent rebuild of cyclic components is required before claiming the complete set was built against candidate dependencies.

Consumers validate artifact run ID, attempt, target, frozen catalog entry, checksums, and Debian metadata. They reconstruct an APT repository and supply verified binaries to sbuild. Required versions are checked against buildinfo. Artifacts from older attempts are rejected; retry the whole workflow for dependency chains.

## Tests and reports

Autopkgtests use separate QEMU guests with 4 GiB RAM and two CPUs on KVM-capable GitHub runners. Built source packages and exact candidate binaries are tested. PASS, FAIL, SKIP, NO_TESTS, INFRA_ERROR, and BLOCKED remain distinct; missing or skipped tests are not passes.

Artifacts `build-SOURCE` and `autopkgtest-SOURCE` contain tar bundles of results and logs. Small `status-*` artifacts feed `pipeline-summary`. Retention is 14 days.

The workflow runs on pushes to `codex/hibiscus-snapshot-pipeline` and manual dispatch. Empty `sources` selects the catalog; a comma-separated pilot list selects exactly those packages and records outside dependencies as archive bootstrap inputs. Recurring nightly scheduling remains pending validation and integration onto the default branch.
