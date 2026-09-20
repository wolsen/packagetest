# Packaging executor design

The packaging tools determine how a Debian package is built. The agent selects reviewed inputs, enforces stage checks, and records enough evidence to explain a failed run.

## Execution boundary

`packaging plan` discovers OpenStack release candidates and a small configured dependency graph. It is advisory. `packaging build --plan LOCK` consumes a separate schema-v1 lock and does no release discovery during execution. This prevents retries from silently changing their source inputs.

`locked.py` implements the stage sequence:

1. Verify tools and the explicitly named schroot.
2. Download a checksum-pinned archive source, or prepare a checksum/commit-pinned upstream update in an isolated Git checkout.
3. Verify source identity and every file referenced by its `.dsc`.
4. Run sbuild in a fresh tarball-backed schroot, with package tests enabled.
5. Validate actual binary metadata, checksummed `.changes` contents, required binaries, and `.buildinfo` dependency versions.
6. Run Lintian, failing on errors.

The workflow additionally calls `smoke-install.py`. This creates another fresh schroot, installs the built packages, checks the installed version, and exercises Oslo translation. The build manifest's `SUCCEEDED` result covers building and artifact validation; `smoke-install.json` records installation separately. The wrapper and workflow require both to pass.

## Source preparation

For archive rebuilds, the reviewed `.dsc` digest anchors component checksums. This executor does not independently authenticate `.dsc` signatures.

For upstream updates, the lock separates the upstream project from the Debian source and binary names. It pins packaging, upstream, and pristine-tar commits and an official release sdist. Reusing an existing import requires the pinned peeled tag commit and identical pristine-tar bytes. Creating an import requires the tag to be absent. Neither mode overwrites published tags.

The executor checks source identity, branch configuration, upstream version, and epoch preservation. It merges upstream, validates the patch queue, returns to the packaging branch, updates and commits the changelog, and requires a clean checkout. gbp exports a fresh source tree and runs unsigned source generation. The deliberate `-nc` source-build policy applies to that fresh export; sbuild performs the normal binary build and tests with resolved build dependencies.

## State and evidence

Each invocation creates a unique generation directory tied to the canonical lock hash. JSON manifest updates use atomic replacement. Per-command logs retain complete stdout/stderr on disk, bounded diagnostic tails in JSON, exit codes, durations, and process-group timeouts. Failure bundles capture the failing stage, packaging files, patches, Git state, and command diagnostics.

Source and binary artifacts are stored separately. A zero exit code with missing, corrupt, or incorrectly versioned binaries fails validation. Source hashes, binary hashes, tool versions, builder identity, and installed build dependency versions are retained.

For ordered multi-package locks, only validated successful producer binaries are passed to consumers using sbuild `--extra-package`. A declared required dependency version is enforced by an sbuild `--add-depends` constraint and must match `.buildinfo`; failed producers block consumers. The pbr → oslo.i18n Stonking case validates this handoff and failure blocking with real runs.

## Scope

Builder profiles cover Noble amd64 (main/universe with updates/security) and Stonking amd64 (main/universe). The archive dependency set is current at build time and recorded, not snapshot pinned. Equal locks therefore do not promise byte-identical packages.

The current GitHub workflow is one job calling the same provisioning/build scripts used locally. It has not yet been validated remotely. Gump workflow integration is deferred; direct runs in an Ubuntu VM establish tool behavior independently.

The earlier planner/scheduler/repository abstractions remain for future work. Repository publication, UCA version policy, service package validation, automatic lock generation, signing, and archive uploads are not connected to this executor. Models may eventually propose fixes from captured evidence; automatic patch application is outside the current design.

## Snapshot inputs

Snapshot acquisition clones full Git history at the locked SHA and verifies the base-tag commit, ancestry, commit count, and timestamp. A separate venv installs checksum-pinned PBR/setuptools/wheel and generates an sdist with the exact snapshot PBR_VERSION. Archive-header normalization makes this generated source repeatable; a lock can require its expected SHA256. The resulting orig is imported using pristine-tar and follows the same source/binary verification path as a release. Installation additionally checks Python distribution metadata retains the complete snapshot version.
