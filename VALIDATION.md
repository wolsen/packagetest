# Completed packaging validation

Validated on 2026-09-20 by running the tool directly in an Ubuntu 24.04 VM. All binary builds used a fresh tarball-backed `noble-amd64-sbuild` schroot. Installation checks used separate fresh schroot sessions.

| Case | Debian version | Build and artifact validation | Fresh install and translation test |
| --- | --- | --- | --- |
| Published Ubuntu baseline | 6.3.0-0ubuntu1 | Passed | Passed |
| Pinned upstream update | 6.4.0-0ubuntu1~packagetest1 | Passed | Passed |
| Same update lock, independent repeat | 6.4.0-0ubuntu1~packagetest1 | Passed | Passed |

Each build produced `python3-oslo.i18n` and `python-oslo.i18n-doc` as actual `.deb` files, plus a binary `.changes`, `.buildinfo`, and sbuild log. All **75 upstream tests passed** in every build. No `nocheck` flag was used. Lintian reported four embedded-JavaScript warnings in the documentation package for both versions, but no errors.

The repeated update used a fresh packaging clone and build root with the same canonical lock hash. All 23 packaged Python files have identical contents across the two update builds. This is not a claim of byte-identical `.deb` files: generated changelog dates, Git commits, and archive dependency selection are not fully reproducible yet.

## Retained evidence

The following directories are present locally under the ignored `artifacts/validated/` directory:

- Baseline: `gen-3cec88208535-86fd6642/`
- Update: `gen-7c6ab950a7e0-2eb8fef7/`
- Update repeat: `gen-7c6ab950a7e0-0c14391e/`

Each contains source/binary artifacts, full command logs, `build-lock.json`, `generation-manifest.json`, and a successful `smoke-install.json`. Artifact checksums and actual `.deb` metadata were revalidated after copying them back to the workstation. The build-time absolute paths in manifests refer to the VM; the retained files use the same relative structure.

Host transcripts are `artifacts/direct-prepare.log`, `direct-baseline.log`, `direct-baseline-install.log`, `direct-update.log`, and `direct-update-repeat.log`. The baseline transcript contains the first smoke-script lookup failure; `direct-baseline-install.log` and the retained successful smoke manifest record the corrected rerun. The initial preparation transcript also contains the corrected working-directory postcheck failure; subsequent executor preflight confirmed the prepared chroot works.

Local agent tests: **56 passed**, also passed inside the VM. Coverage includes real `.deb` inspection, corrupted/missing artifact rejection, dependency blocking, lock validation, process-start failures, and command timeouts. Shell syntax and Git whitespace checks passed.

## Environment retained for reuse

The VM `packagetest-baseline-f590e9` has been stopped to release memory, with its prepared builder retained. The repository is at `/home/runner/packagetest` inside it. It can be restarted with `lxc start packagetest-baseline-f590e9`; then enter it through SSH or `lxc exec`, update the source checkout, and run the scripts documented in README.md. Other disposable VMs created during failed workflow attempts were removed.

## Remaining boundaries

Gump was attempted and then set aside as requested. Three small fixes and their regression tests remain in the sibling checkout and in `integration/gump-packaging.patch`; workflow context propagation still blocked execution of uncommitted edits. Gump and GitHub Actions end-to-end success are **not** claimed.

A real producer/consumer dependency case, UCA target/version policy, service packages, automatic discovery-to-lock conversion, archive snapshots, and uploads remain future work. No pushes, package uploads, or signing were performed. The later implementation checkpoint is recorded below.

## Latest-upstream snapshot, 2026-09-20

A further direct build of upstream commit `7045af07a0654247ad9f3dfd416622afed3e1489` succeeded on **Stonking amd64**, including all 75 upstream tests on Python 3.14 and installation into a separate fresh schroot. Debian version: `6.9.0+git20260914.3.7045af0-0ubuntu1~packagetest1`. Installed Python distribution version: `6.9.0+git20260914.3.7045af0`.

Evidence: `artifacts/validated/gen-e018f791b30e-08323b0e/`; details and repeat instructions: `SNAPSHOTS.md`. Snapshot sdist generation was independently repeated and matched the pinned checksum. Lintian returned no errors; documentation and long-filename warnings are recorded in the logs. Agent tests now total **61 passed**. Gump remains deferred.

## Producer/consumer dependency validation, 2026-09-20

Implementation checkpoint `66ae9df` records the working release/snapshot slice. A subsequent real Stonking build of python-pbr → python-oslo.i18n succeeded in generation `gen-07019ac10c56-d323c648`.

The consumer's binary `.buildinfo` records `python3-pbr (= 7.1.0+git20260813.1.9ec6e72-0ubuntu1+packagetest1)`. The build log shows it fetched from the local sbuild artifact repository, and the manifest records the producer artifact hashes. All four generated `.deb` packages passed metadata/checksum checks and installed at their expected versions in a fresh schroot. The Oslo snapshot's 75 tests and translation/version smoke checks passed.

An independent negative test deliberately changed the producer tarball checksum. Pbr failed source acquisition, oslo.i18n was blocked without checkout or build, and `artifacts/dependency-negative/assertions.json` records the expected outcome. Both positive and negative evidence are retained locally; see `DEPENDENCY-VALIDATION.md`.

Agent tests: **63 passed**. Ubuntu's pinned pbr packaging intentionally skips pbr build-time unit tests; no claim is made that those tests ran. No patches or `nocheck` settings were introduced. The PBR wheel used during snapshot sdist generation remains a separately pinned bootstrap tool; this test proves the Debian producer dependency in the consumer's binary build.
