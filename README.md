# OpenStack packaging agent

A small, deterministic executor for Ubuntu Debian package builds. The tool runs directly in a prepared Ubuntu 24.04 builder. The GitHub workflow calls the same scripts. Each sbuild invocation extracts a fresh Noble schroot.

The first integration cases are deliberately small:

- `baseline`: rebuild Ubuntu's published `python-oslo.i18n` 6.3.0-0ubuntu1 source package.
- `update`: prepare 6.4.0 from a pinned Launchpad packaging commit and official release tarball, then build 6.4.0-0ubuntu1~packagetest1.
- `snapshot`: generate a PBR sdist from pinned oslo.i18n Git commit `7045af07a065`, then build it for Stonking. See `SNAPSHOTS.md`.

The executor validates checksums, actual `.deb` metadata, `.changes`, and `.buildinfo`; runs Lintian; and records installed build dependency versions. The workflow installs the resulting packages in another fresh schroot and exercises Oslo translation. A successful subprocess without the expected artifacts is a failed build.

## Run locally

Use a disposable Ubuntu 24.04 VM with passwordless sudo, at least 4 CPUs, 8 GiB RAM, and 40 GiB disk. Copy this repository into it, then run:

```bash
bash scripts/prepare-builder.sh
bash scripts/run-library.sh baseline
bash scripts/run-library.sh update

# Latest upstream snapshot pinned on 2026-09-20, using current Ubuntu packaging
bash scripts/prepare-builder.sh stonking
bash scripts/run-library.sh snapshot
```

The provisioning script installs packaging prerequisites and creates the target schroot. Run it in the VM, not on a workstation you want to keep unchanged. The build script installs the agent, runs its unit tests, builds the selected package, and verifies installation in a fresh schroot.

Gump was tried but its workflow context handling prevented validation of uncommitted changes. We switched to running these commands directly in an existing Ubuntu VM. Gump integration remains deferred; see `GUMP-INTEGRATION.md` for the small fixes already made and the remaining issue. GitHub Actions execution is also pending remote validation.

## Run the executor in a prepared builder

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-build-isolation -e .
.venv/bin/packaging build --plan config/locks/oslo-i18n-noble-baseline.json --dry-run
sg sbuild -c '.venv/bin/packaging build --plan config/locks/oslo-i18n-noble-baseline.json'
.venv/bin/python scripts/smoke-install.py artifacts
```

`--dry-run` validates the lock and reports `PLANNED`; it never reports a successful build. The CLI intentionally requires a reviewed schema-v1 lock. The separate `packaging plan` command discovers candidate releases and dependencies; its output is **not** yet an executable lock. The old `build --openstack-target ...` interface has been removed.

A generation directory contains:

- `build-lock.json` and `generation-manifest.json`, with exact input identity and results.
- `artifacts/<source>/source/` and `binary/`, including tarballs, `.dsc`, `.deb`, `.changes`, `.buildinfo`, and sbuild logs.
- Full numbered stdout/stderr logs, a command JSONL index, and stage transitions.
- Failure bundles containing actual packaging files, patches, Git state, and command diagnostics.
- `smoke-install.json` after the separate installation check. Overall workflow success requires this check as well as build success.

## Scope and reproducibility

Release tarballs and archive source inputs are checksum pinned; Git inputs are commit pinned. The build records dependency versions but uses the current Noble, Noble updates, and Noble security archives. This is not yet a snapshot-pinned dependency environment or a claim of bit-for-bit reproducibility. Source integrity relies on reviewed SHA256 locks obtained over HTTPS; archive `.dsc` signatures are not independently authenticated by this executor.

The update path honors the selected packaging and upstream branches, requires explicit `reuse` or `new` import policy, verifies reused pristine-tar content, validates patch application, commits the changelog, and builds unsigned source artifacts. It fails on conflicts instead of asking a model to improvise packaging policy.

Dependency handoff with sbuild `--extra-package` and required build-version checks is implemented, but a real producer/consumer integration case remains to be established. Discovery/scheduler/repository modules from the earlier prototype remain available for development; repository publication is not wired into the locked executor. UCA policy, service packages, automatic remediation, signing, and Launchpad uploads are future work.

## Development

```bash
PYTHONPATH=src python3 -m pytest -q
```

`ASSESSMENT.md` records the original diagnosis; `NEXT-STEPS.md` records the broader implementation sequence. They are historical planning documents, not evidence that every milestone is complete.
