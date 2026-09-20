# About the packaging agent's build failures

Assessment of local commit `c763f53`, prepared 2026-09-19 (America/Phoenix).

The project has useful planning, command logging, and manifest scaffolding, but the packaging executor is not yet a working implementation of the Ubuntu OpenStack workflow. Changing the workflow runner will not fix its packaging errors. The recommended direction is a deterministic executor using `gbp` and `sbuild`, first proven on one pinned package in an Ubuntu VM, then invoked by GitHub Actions. The user's local **Gump** project is the better candidate for VM-based workflow debugging once its timeout and artifact-lifetime gaps are addressed. `nektos/act` remains a viable alternative local harness.

The proposed implementation sequence is in [NEXT-STEPS.md](/home/wolsen/work/playground/packagetest/NEXT-STEPS.md). These documents record an assessment and proposal; no application code or workflow was changed.

## Evidence and limits

- Read the project's source, workflow, configuration, tests, README, design, and roadmap, and both supplied packaging guides. The guides were treated as reference material, not authorization to execute their commands.
- Ran `PYTHONPATH=src pytest -q`: **64 passed, 1 failed**. The failing test expects the old `/+source/pbr` repository URL; configuration now uses `/+source/python-pbr`.
- Ran the read-only plan command with `--openstack-target 2026.1 --ubuntu-release noble glance`. It succeeded and selected Gazpacho releases: pbr 7.1.2, oslo.i18n 6.7.2, oslo.serialization 5.9.1, and glance 32.0.0. These are observations of live metadata, not proposed pinned inputs.
- Inspected public metadata for [packaging run 35418969528](https://github.com/wolsen/packagetest/actions/runs/35418969528), at the local commit. Its `Create plan artifact` step failed; all three build jobs were skipped and no artifacts were uploaded. Full logs returned HTTP 403 without authentication. The precise historical planning exception remains unknown.
- The more recent green [run 35419094205](https://github.com/wolsen/packagetest/actions/runs/35419094205) is named `Running Copilot cloud agent`; it is not evidence of a successful packaging workflow.
- Read live Launchpad packaging metadata and checked upstream tool documentation. Reproduced the absolute-filename APT index issue with a disposable minimal package in `/tmp`.
- Did not run a real OpenStack source/binary build or an `act` workflow. Runner capability below is a documentation-based assessment, not a successful end-to-end demonstration.
- After the user identified `/home/wolsen/work/playground/gump`, inspected its implementation at commit `a2f43b9`. Parsed and compiled all four packagetest jobs through Gump's actual parser/planner, including typed dispatch inputs and runner-profile resolution. All passed: plan 5 steps, pbr 9, Oslo 10, Glance 10. No VM was provisioned or workflow executed.
- Ran Gump's artifact store/API test modules: **13 passed**, with one dependency deprecation warning. The in-process API test initially stalled under the sandbox; a bounded rerun outside it completed in 0.35 seconds. These unit checks do not replace a live runner/action transfer test.

## Problems before the binary build

| Finding | Evidence | Consequence |
| --- | --- | --- |
| Source identity is inconsistent | Configuration calls the node `pbr`, while the selected Launchpad repository's `debian/control` declares `Source: python-pbr`. `packaging.py:25` also strips `python-` from orig filenames for every library. | Upstream project name, Debian source name, and binary names are conflated. Orig filenames must follow the Debian source identity; the internal tarball directory can use the upstream name. |
| Branch selection ignores the release | `packaging.py:68` selects remote HEAD, then lines 90–93 require `origin/upstream`. Import/build commands also force `upstream`. | The live python-pbr configuration specifies `upstream-hibiscus`, while the requested plan is Gazpacho. Default HEAD is not a release policy. |
| The source acquisition policy is unused | `source_creation_method` is recorded but execution always performs `git archive` (`packaging.py:84`). | The configured release-tarball/sdist strategy is not implemented. Git archives omit generated release metadata that PBR-based packaging can need. Re-creating release tarballs can also conflict with existing pristine-tar data. |
| The import is not idempotent | Every run calls `gbp import-orig`; it does not recognize an already-imported matching version. | Existing tags/imports can fail instead of being reused. Verification must compare content/provenance, not simply delete tags or overwrite imports. |
| Patch-queue lifecycle is incomplete | `gbp pq import` is immediately followed by `dch` and `gbp buildpackage` (`packaging.py:150`). | Successful import switches to the patch-queue branch. There is no return to the packaging branch or export of intentional patch changes. [gbp pq manual](https://manpages.debian.org/unstable/git-buildpackage/gbp-pq.1.en.html). |
| Changelog changes are not committed | The command list changes `debian/changelog` and immediately builds, without a commit or an explicit working-copy export policy. | `gbp` normally rejects the dirty tree. Ignoring that check alone would leave the branch and exported-content problems unresolved. |
| Version policy loses information | `versioning.py` produces `<converted-upstream>-0ubuntu1` without inspecting the existing changelog. Snapshot resolution changes the SHA but retains the release version. | Epochs can be lost, updates can sort backwards, and different snapshots can share a package version. Prerelease orig/import versions also use raw PEP versions while the changelog uses converted Debian versions. |
| Source-build environment is implicit | The builder is `debuild -S -sa`, with no explicit unsigned mode, dependency-check policy, Git author configuration, or maintainer environment. | A clean runner can fail on identity, host build dependencies/clean hooks, or signing before reaching `sbuild`. |

The live [python-pbr gbp.conf](https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/python-pbr/plain/debian/gbp.conf) specifies `master`, `upstream-hibiscus`, and `export-dir = ../build-area`. Its [control file](https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/python-pbr/plain/debian/control) confirms the source identity. These moving files should ultimately be read at a pinned packaging commit.

## Problems in builds and dependency delivery

**The `sbuild` command is wrong.** `packaging.py:104` uses `--build=source+all+any`. For `sbuild`, `--build` takes an architecture, such as `amd64`; source/all/any selection uses separate options. For the initial native binary build, use an explicit target and architecture, `--arch-all`, and a concrete `.dsc` path. Add `--source` only when a source rebuild is intended. This was checked against the installed manual and the [sbuild manual](https://manpages.debian.org/unstable/sbuild/sbuild.1.en.html).

**Output paths are inconsistent.** `gbp` can export into `../build-area`, but the executor passes `../*.dsc` to `sbuild` and searches only the packaging checkout's parent for artifacts. `sbuild` also has a separately configurable build directory. Both output directories need explicit ownership; the `.dsc` path must come from validated source-build results. Broad globs are not a reliable handoff.

**Build success does not prove a binary exists.** `_package_has_publishable_outputs()` in `cli.py:581` checks only for a `.dsc`. A source-only output can qualify for publication with an empty binary pool. Tests create placeholder text files named `.deb` and `.dsc`; they do not validate package contents or perform real package builds.

**In-process dependencies are released too early.** `BUILD_SUCCEEDED` is a dependency-ready state, but `_publish_run_outputs()` runs only after the scheduling loop. Current-generation binaries are not passed to downstream builds. Either make validated binaries available immediately through `--extra-package`, or publish and verify their repository before scheduling dependents.

**Cross-job repositories are not portable yet.** The code adds `file://` repositories without configuring their visibility inside the chroot. Independently, `apt-ftparchive packages` receives an absolute pool path. A local probe confirmed it emits an absolute `Filename:` field containing the producer's directory. Generate indexes from relative `pool/` paths, then test the repository after relocating it. Glance's job downloads the Oslo generation but not the pbr generation, so the original ancestor artifact is also absent from its input set.

For the first two-package chain, `sbuild --extra-package=/absolute/path/to/dependency.deb` is simpler: sbuild copies it into the build environment and exposes it for dependency resolution. This still requires checking the installed dependency version; availability alone does not guarantee selection. [Ubuntu local build guide](https://ubuntu.com/project/docs/contributors/building/build-packages-locally/).

**Signing is incomplete.** Repository generation creates a GPG key in the default keyring without a run-specific `GNUPGHOME` or explicit unattended passphrase setup. Killing `gpg-agent` does not delete keys. Meanwhile consumers use `trusted=yes`, bypassing the signature verification this infrastructure is meant to provide. The initial artifact-injection path can defer custom repository signing entirely.

**Runner provisioning needs a preflight.** The workflow installs packages and creates a schroot but does not explicitly select the backend, validate archive components/pockets, or refresh the build process's supplementary groups after `sbuild-adduser`. These are environment risks to test, not observed causes of the inspected planning failure. Pin the runner OS and toolchain rather than relying on `ubuntu-latest`.

## The agent and orchestration model

The repository contains a deterministic Python orchestrator and placeholder remediation artifacts; it does not yet contain an AI repair adapter. That is a useful boundary to retain. Routine package mechanics should become explicit code with stage postconditions. An AI component can later analyze a failed stage and propose a bounded patch using complete evidence.

Several advertised guarantees are not implemented:

- The workflow downloads `plan.json`, but `build` has no plan-input argument and re-resolves live metadata. Every invocation creates a new generation ID.
- The two Oslo commands are sequential in one job, despite the README describing parallel work.
- The dependency graph is hand-authored and incomplete; it is not derived from Debian build dependencies or archive satisfiability.
- Failure bundles pass empty strings for packaging files, and upstream SHAs are often null. `build_dependency_versions` is never populated.
- Command output is buffered until process completion, with no timeout or handling of process-start errors. Verbose mode is not live build-log streaming.
- Release discovery parses YAML using regular expressions, retains only the first project per release, and chooses current independent releases without a target constraints policy. Snapshot requests do not freeze release metadata or packaging refs.

The roadmap should describe milestones 2 and 3 as implemented scaffolding awaiting integration acceptance, rather than completed packaging capability.

## Runner decision

| Option | Suitability | Recommendation |
| --- | --- | --- |
| Direct executor in a disposable Ubuntu VM | Matches Linux packaging requirements without nested container/chroot complications. | Best first debugging environment. Keep the same command entry point for hosted CI. |
| GitHub-hosted Ubuntu runner | Can provision standard Ubuntu packaging tools and clean build environments. | Keep as the CI execution platform; first prove one job and one package. |
| `nektos/act`, Docker mode | Can run the workflow structure, but containers differ from hosted VMs. Nested schroot mounts or user namespaces need explicit runtime support. | Useful after the executor works. Use a suitable pinned Ubuntu image, test backend permissions, and enable the artifact server. |
| `nektos/act`, native mode inside a disposable VM | Runs jobs directly in the VM, avoiding an extra Docker layer. | Reasonable local workflow option if YAML parity is valuable. |
| User's local Gump, `lxd-vm` | Official Actions runner inside a real Ubuntu VM, local workflow orchestration, v4 artifact service, and failure-preservation support. | Preferred local workflow candidate for this project, after the specific gaps below; keep GitHub-hosted CI as the acceptance environment. |
| User's local Gump, `lxd-container` | Shares the host kernel and ships as an unprivileged system container. | Do not choose this backend for the initial schroot build; its suitability requires additional nesting/mount validation. |

Act supports custom runner mappings, including `-P ubuntu-latest=-self-hosted`. This label means local execution by act, not registration of a persistent GitHub runner. Its default container images are incomplete compared with GitHub's VMs. [Act runner documentation](https://nektosact.com/usage/runners.html).

The current upload/download-artifact v4 usage is not itself a reason to reject act: its local artifact server supports v4 transfer within a workflow when enabled with `--artifact-server-path`. [Act artifact documentation](https://nektosact.com/usage/#action-artifacts).

For the existing schroot approach in Docker, the practical candidate is an appropriately privileged container inside a disposable VM. Exact capability/security-profile requirements need a smoke test on the chosen host. The current [act CLI source](https://github.com/nektos/act/blob/master/cmd/root.go) exposes `--container-options`; its older `--privileged` flag is deprecated in favor of passing `--privileged` through those options. Unshare instead needs working user namespaces and UID/GID mappings; it should not be assumed to work inside an arbitrary container.

After the executor fixes, a candidate diagnostic invocation is:

```bash
act workflow_dispatch \
  -W .github/workflows/vertical-slice.yml \
  -j build-pbr \
  -P ubuntu-latest=-self-hosted \
  --input openstack_target=2026.1 \
  --input ubuntu_release=noble \
  --artifact-server-path "$PWD/.act-artifacts"
```

This is an untested invocation intended for a disposable, provisioned Ubuntu VM. It illustrates workflow selection and artifact support, not a claim that the current workflow works or that this target pair has been validated. Docker mode would replace the native mapping with an appropriate pinned Ubuntu image and verified container options.

The intended Gump is the user's [local control-plane project](/home/wolsen/work/playground/gump/README.md), not Apache Gump. Its code pins the official runner to `2.337.0`; the built-in `ubuntu-latest` profile selects an Ubuntu 24.04 LXD VM. That provides the system environment this packaging workflow expects without nesting schroot inside a Docker runner. Its real-runner execution and `--preserve failed` support make it a good debugging fit. The parser/compiler check above confirms compatibility with this workflow's present syntax; it does not verify action execution, sbuild, or runner-image readiness.

Three implementation details matter before relying on it:

1. **A hard 15-minute inner wait.** [runner/lifecycle.py:59](/home/wolsen/work/playground/gump/src/gump/runner/lifecycle.py:59) waits at most 900 seconds for job completion. A larger YAML `timeout-minutes` only wraps this operation and cannot extend the inner cap. Cold package installation, chroot creation, and builds can exceed it. Use one effective configurable job timeout, and ensure expiry stops the worker/build before reusing or preserving the VM. The current inner timeout returns `None`, which is not the outer `cancelled` cleanup path.
2. **Artifacts and logs are process-lifetime storage.** [runstate/artifacts.py](/home/wolsen/work/playground/gump/src/gump/runstate/artifacts.py:1) holds upload blocks and complete ZIP bytes in memory; teardown loses them. This supports same-run handoff, but not durable evidence or later retrieval. Packagetest currently uploads all of `artifacts/`, which also contains source checkouts and workspaces. Narrow uploads to actual outputs/logs/manifests, and persist/export artifacts to disk before control-plane exit. The code uses a shared `local:local` artifact namespace, so it is not a multi-tenant service.
3. **Runner identity is ephemeral; the machine is reused.** [runner/lifecycle.py:131](/home/wolsen/work/playground/gump/src/gump/runner/lifecycle.py:131) releases completed machines back into the pool and destroys them at run teardown. With one worker, repeated `sbuild-createchroot` calls target an existing directory. More broadly, apt/chroot/workspace state can leak across jobs. Prefer fresh per-job machines/reset snapshots for parity, or make provisioning explicitly idempotent and retain clean per-build sbuild isolation. Preserve-policy removal of a pool slot also needs a replacement/fail-fast policy before unrelated parallel work depends on that slot.

Gump's baseline image does not include the packaging toolchain; provisioning still needs to install it. Its VM improves environment fidelity but does not reproduce GitHub's complete image or hosted services automatically. Use a single-job packaging smoke workflow first. The existing producer/consumer [artifact fixture](/home/wolsen/work/playground/gump/tests/workflows/artifacts-e2e.yaml) is useful for a separate live handoff check.

After those fixes and image provisioning, a candidate full-workflow invocation is:

```bash
/home/wolsen/work/playground/gump/.venv/bin/gump run \
  /home/wolsen/work/playground/packagetest/.github/workflows/vertical-slice.yml \
  --worktree /home/wolsen/work/playground/packagetest \
  --provider lxd-vm --name packagetest-smoke \
  --event workflow_dispatch \
  --input openstack_target=2026.1 --input ubuntu_release=noble \
  --max-parallel 1 --preserve failed \
  --cpus 4 --memory 8GiB --disk 40GiB
```

This command is illustrative and was not run. Start with a reduced single-package workflow rather than this full chain. Explicit `--provider` currently bypasses profile-based image selection and falls back to Ubuntu 24.04; for another runner OS, select a matching configured profile without that override. Provisioning a VM or starting Gump was unnecessary to identify the code-level blockers in this assessment.

## Ubuntu, UCA, and the supplied guides

Ubuntu development packaging and UCA backporting need separate target profiles. A single `ubuntu_release` string cannot describe the base suite, OpenStack series, archive pockets, permitted PPAs/UCA repositories, architecture, packaging branch, and version suffix policy. Building current packaging on Noble is not sufficient to establish a valid UCA package.

The manual guide gives useful sequencing: obtain a release tarball or generate an sdist, respect `gbp.conf`, manage patches, commit metadata, build source, then run sbuild. It also describes another project, `packastack`; those claimed capabilities are not evidence about this repository.

The references contain details that should not become automatic rules. For example, testing reverse patch application on a patch-queue tree where the patch has already been applied does not prove upstream included it. The guide's `-A` description says “only” architecture-independent packages, but sbuild documents `-A` as “also”; exclusive arch-all selection requires disabling arch-any. Its local repository example also assumes a mount mapping not established by the shown path alone. Version epochs should be preserved, not invented to hide a target-selection mistake. The two guides disagree about whether the helper applies patches; inspect the exact helper version before delegating semantics to it.

The first delivery should establish one repeatable `.deb` build with validated contents. Target expansion, snapshots, dependency relaxation, patch repair, and eventual Launchpad publication should be layered onto that evidence.
