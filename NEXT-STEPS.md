# Plan for a working OpenStack packaging agent

This is a proposed implementation sequence based on [ASSESSMENT.md](/home/wolsen/work/playground/packagetest/ASSESSMENT.md), not a record of completed fixes. The first milestone is one verified library binary build; Glance and the full dependency graph come later.

## 1. Establish a reproducible baseline

- Choose one small existing library, preferably `python-oslo.i18n`, and a source version already published for the initial Ubuntu suite. First rebuild its existing `.dsc` with sbuild; do not combine environment validation with an upstream upgrade.
- Provision a disposable Ubuntu VM and an equivalent pinned GitHub-hosted job. Explicitly select one sbuild backend. Record tool versions, architecture, archive components/pockets, chroot identity, and effective group membership.
- Prefer the local Gump project's `lxd-vm` backend for the workflow smoke test after removing its hard 900-second inner job wait. Use a reduced single-job workflow initially. Add durable artifact/log export and make machine freshness/provisioning policy explicit before running the full multi-job chain. See the Gump findings in the assessment; its four-job parse/compile check already passes.
- Retain the `.dsc`, orig/debian tarballs, binary `.changes`, `.buildinfo`, `.build` log, and resulting `.deb` files.
- Inspect binaries using `dpkg-deb`; verify their names, versions, architecture, and checksums against `.changes`. Install them and run a package-appropriate smoke test in a fresh target environment.
- Preserve the exact baseline inputs as an integration fixture/manifest. Label local and CI runs with the same input identity.

Acceptance: the same existing source package builds and passes artifact validation locally and on GitHub. This distinguishes environment failure from source preparation failure.

## 2. Implement one pinned packaging update

- Select a compatible upstream version and packaging commit for that library and suite. Freeze them explicitly rather than resolving “latest” on every invocation.
- Keep upstream project, Debian source, and binary names separate. Validate `Source:` and the changelog source field. Correct the pbr node and tests together when bringing pbr back into scope.
- Read effective `gbp.conf`, with explicit target-profile overrides and a record of their resolved values. Track the required branches and reject mismatched series before mutation.
- Use the official release sdist for release builds, with recorded URL and digest and signature verification where available. Use `<Debian-source>_<Debian-upstream-version>.orig.tar.*`; preserve the raw upstream version/tag separately.
- Reuse an existing import only when it matches the pinned source; otherwise import into an isolated checkout. Do not delete published tags to make a run pass.
- Complete the patch lifecycle: validate on the new source, export only intentional changes, return to the packaging branch, and commit changes separately from the changelog. Preserve epoch and enforce Debian version ordering.
- Configure repository-local Git identity and noninteractive maintainer values. Build unsigned source artifacts with an explicit output directory. Provide the host tools needed for source clean hooks or deliberately use the appropriate clean-source policy; disabling dependency checks alone does not supply those tools.
- Replace the invalid sbuild option, pass the one validated `.dsc` path, and set the binary output directory explicitly.
- Implement source and binary artifact validation as stage postconditions. Missing or corrupt expected binaries must fail the run even if subprocesses returned zero.

Acceptance: the agent produces a real upgraded `.deb` from an isolated checkout twice using the same locked inputs. Both runs meet the same package/version/content checks. Byte-for-byte reproducibility is a separate later measurement.

## 3. Make failures actionable

- Stream stdout/stderr to stage log files and retain command metadata, exit status, duration, and bounded timeouts. Record process-start errors and interruptions as failures too.
- Include actual `debian/control`, `rules`, `changelog`, `gbp.conf`, patch series and relevant patches, Git state, exact source/package SHAs, and sbuild logs in failure bundles.
- Separate acquisition, branch/configuration, import, patch, source-build, environment, dependency-resolution, binary-build, test, and publication failures. A namespace failure is not a compiler failure.
- Upload planning output and diagnostics even when planning fails. Download the historical failing CI log with authenticated access if available, without treating a successful rerun against changed metadata as an explanation of the original failure.
- Reopen roadmap completion claims until their acceptance checks pass.

Acceptance: each intentional failure is attributable to a specific stage and has enough evidence to reproduce it; diagnostics survive unsuccessful CI runs.

## 4. Prove one dependency edge

- Build a verified producer and consumer pair, such as python-pbr and a compatible Oslo library.
- Initially hand off validated `.deb` artifacts using sbuild's `--extra-package`, avoiding custom APT repository signing and chroot mounts during bootstrap.
- Inspect `.buildinfo`/build logs to assert the consumer installed the intended producer version. For the integration scenario, choose a dependency constraint that cannot silently be satisfied by an unrelated archive version.
- Require usable dependency artifacts before scheduling a consumer. Preserve transitive inputs and record selected dependency versions.
- If retaining the repository backend, generate relative `Filename: pool/...` entries, relocate the repository, and verify APT installability inside a clean chroot. Choose explicit mount/network access and signature trust, with run-local keys and cleanup where signing is used.

Acceptance: a consumer proves it used the freshly built producer, and a failed/missing producer blocks that consumer.

## 5. Make the plan executable and CI thin

- Add a versioned lock/plan format and a `build --plan <file>` entry point. Include packaging SHA/branch, upstream SHA, release metadata revision, tarball identity, full Debian version, target profile, and dependency artifact identities.
- Resolve once; builds consume the lock without silently re-resolving upstream or creating unrelated generation identities. Record APT repository snapshots or the actual resolved binary versions and hashes to delimit reproducibility.
- Generate job/package selection from the plan or keep one sequential job initially. Consolidate duplicated runner setup. Introduce Oslo fan-out only after serial artifact delivery works.
- Run the unit suite on every change. Add real-tool integration coverage for branch handling, pristine-tar reuse, a clean source build, binary validation, and a dependency handoff. Keep a small offline Git/package fixture for mechanics and a pinned OpenStack smoke build for reality.
- Assess act using the same successful executor. Verify workflow inputs, selected image/native runtime, backend permissions, and v4 artifact transfer. Record exactly which configuration passed.
- Run Gump's real producer/consumer artifact fixture and then the packaging handoff. Prove output retrieval after Gump exits and verify that no stale chroot or checkout state supplies a dependency accidentally. Fix pool replacement/fail-fast behavior before combining preserved failures with independent parallel jobs.

Acceptance: one lock produces equivalent validated results in the direct VM executor, Gump VM workflow, and hosted CI. Act is an optional alternative accepted only after its own smoke test. A runner-specific failure does not invalidate an otherwise working packaging backend.

## 6. Add Ubuntu/UCA target policy and agent assistance

- Define separate development-Ubuntu and UCA profiles: base suite, OpenStack series, architecture, packaging refs, permitted archive pockets/PPAs, and Debian revision/backport suffix rules. Validate supported combinations from current Ubuntu/team metadata.
- Derive dependency work from Debian source metadata and actual archive satisfiability. Handle Build-Depends-Indep/Arch, alternatives, profiles, runtime dependencies where needed, and binary-to-source mapping. Treat dependency cycles as explicit bootstrap decisions.
- Add snapshot support only with pinned commits, generated sdists, distinct monotonic snapshot versions, PBR-compatible metadata, and tests for reruns and existing imports.
- Introduce AI analysis after evidence collection works. Let it propose a patch with a failure explanation and validation result from a disposable branch. Preserve the existing review boundary for changes to patches, dependency minima, tests, or packaging policy. Do not automatically weaken dependencies or disable tests to obtain a green run.
- Add Glance after the libraries and target archive are viable. Run relevant package tests, lintian, installation/upgrade checks, and available autopkgtests.
- Treat eventual source signing, Launchpad merge proposals, PPA proof builds, and archive promotion as a separate delivery feature. Source-only upload and publication policy must be explicit; local `.deb` success alone does not establish UCA acceptance.

Acceptance: one supported target profile has a complete library-to-service build with recorded dependency versions and reviewable changes. New targets are added through the same validation gates.

## Suggested first implementation change

Implement the single-package baseline and executor corrections in steps 1–2, with diagnostic capture from step 3. Defer full DAG fan-out, custom APT signing, snapshot discovery, and AI repair until a real OpenStack `.deb` is validated. Keep GitHub-hosted CI; use direct VM execution or Gump's VM backend for local diagnosis, with the small Gump readiness fixes tracked separately from package correctness.
