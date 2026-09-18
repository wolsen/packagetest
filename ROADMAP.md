# ROADMAP

## Milestone 0 — Foundations (completed in this slice)

- [x] Define architecture and boundaries for real-tool packaging orchestration.
- [x] Add planner/scheduler with dependency-state model.
- [x] Add generation manifest and failure bundle artifact contracts.
- [x] Add declarative package configuration for small OpenStack chain.
- [x] Add CI workflow skeleton showing dependency-aware fan-out.

## Milestone 1 — Release discovery and source selection

- [ ] Pull release metadata from `openstack/releases` deliverables and series status.
- [ ] Resolve targets (`2027.1`, `-b1`, `-rc1`, `-final`, snapshots) into project versions/SHAs.
- [ ] Add reproducible snapshot mode (`snapshot-at=<timestamp>`) with recorded upstream SHAs.

## Milestone 2 — Real packaging repository operations

- [ ] Clone Ubuntu packaging repositories and checkout correct packaging/upstream/pristine-tar branches.
- [ ] Execute `gbp import-orig` with branch-aware options.
- [ ] Execute `gbp pq import` and classify patch failures.
- [ ] Update `debian/changelog` and package metadata with Debian version semantics.

## Milestone 3 — Real source and binary builds

- [ ] Generate source packages and build binaries with `sbuild` on GitHub-hosted runners.
- [ ] Persist source/binary artifacts and logs as generation-scoped artifacts.
- [ ] Ensure dependent builds consume prior-generation packages through standard APT semantics.

## Milestone 4 — Failure intelligence

- [ ] Collect full failure evidence bundles by failure category.
- [ ] Add optional AI analysis adapter producing review-only patch proposals.
- [ ] Add PR-oriented output format suitable for human review workflows.

## Milestone 5 — Scale-out and hardening

- [ ] Expand package set beyond vertical slice.
- [ ] Improve scheduler for larger DAG layers and retries.
- [ ] Add stronger integration tests using temporary git repositories/chroots.
- [ ] Harden security and provenance verification in CI.
