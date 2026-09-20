# Packaging milestones 2–5

Implementation and validation on 2026-09-20. Tasks 2–5 are complete. Work is on `codex/packaging-milestones`; the executor runs directly in an Ubuntu VM and in GitHub-hosted Actions. Gump remains deferred.

## 2. Snapshot lock creation

`packaging lock-snapshot` accepts a branch and optional timestamp cutoff, preserves the template's packaging and target pins, resolves the full source commit and release-tag ancestry, derives the Debian/Python version, and generates a checksum-pinned PBR sdist before writing a new lock. Existing locks are never overwritten or silently refreshed.

Both real discovery cases completed, and both resulting source versions built and passed fresh-install translation and Python-version checks:

| Selection | Upstream version | Portable sdist SHA256 |
| --- | --- | --- |
| `master` | `6.9.0+git20260914.3.7045af0` | `de75efaa73e20b6db3f918b9e4641d944a4b8a6b9246edb207fbfa3236ec55b9` |
| Cutoff `2026-09-10T00:00:00Z` | `6.9.0+git20260903.1.b3b3686` | `03fe3ccc618bd5bc4b9d85a2e6e5fc23c9c1218968735e05e64601682d6d85c4` |

Resolution reports, generated locks, sdists and command logs are retained under `artifacts/resolutions/`. Cutoff selection uses first-parent committer timestamps, not historical server-side branch ref records. See [SNAPSHOTS.md](SNAPSHOTS.md) for the interface and limitations.

The hosted run caught a real portability defect: source file contents matched, but the VM and GitHub had different umasks. The `portable-v1` archive recipe normalizes modes while retaining executability. A regression test covers this; independently normalized local and hosted archives have identical bytes. The checked-in locks explicitly record the new format and digest. Legacy locks retain their original recipe.

## 3. Noble / UCA target

The `noble-uca-epoxy` profile specifies the base OS, OpenStack series, stable packaging branches, signed UCA repository, allowed pockets, package distribution and cloud revision policy. It distinguishes base suite `noble` from package distribution `noble-epoxy`.

The pinned `stable/2025.1` Oslo packaging branch built `6.5.1-0ubuntu1.1~cloud0+packagetest1`, passed all 75 upstream tests, produced both expected `.deb` files, passed checksum/metadata and Lintian error checks, and installed successfully in a fresh chroot. The translation smoke test passed. The executor checks version advancement relative to the pinned prior UCA version and exact correspondence with the Ubuntu packaging base.

Evidence: `artifacts/validated/gen-43ed5d59bd6e-db942a61/`. Checksums and Debian metadata were verified again after transfer to the workstation. The cutoff build is `artifacts/validated/gen-785abb01af22-22a74e54/`. The local snapshot's final portable-format generation is `artifacts/validated/gen-c68ab9a34fe7-d9572630/`.

Noble's Lintian lacks UCA pocket names. This profile extends its distribution data with `noble-epoxy`; the extended file and its hash are retained. No Lintian checks are disabled. Effective APT sources and actual build dependency versions are recorded. Dependencies are not yet served from immutable archive snapshots. See [UCA.md](UCA.md).

## 4. GitHub Actions

The validation matrix covers baseline, upstream update, snapshot, dependency handoff, UCA backport and Glance. It uses the same scripts as local execution and uploads diagnostics even when a job fails. The dependency case also intentionally corrupts the producer checksum and asserts that the consumer is blocked.

Early hosted runs exposed the umask issue above and an artifact transport issue: GitHub rejects colons in sbuild log filenames. The workflow now uploads a tar bundle, preserving original names, permissions and symlinks. Package and install checks had already passed in the early baseline/update jobs; their job failure was specifically artifact upload.

All **six hosted jobs passed** in [run 35498752368](https://github.com/wolsen/packagetest/actions/runs/35498752368), code commit `cc823e63efe3b8a12cdbc6ad1db5091ab33ef6e2`. All six artifact bundles were downloaded and independently verified, covering **17 binary artifacts**. Glance's hosted image round trip passed, and the dependency job's intentional producer failure correctly blocked the consumer.

| Hosted case | Build, installation and artifact upload |
| --- | --- |
| Noble published baseline | Passed |
| Noble pinned upstream update | Passed |
| Stonking snapshot | Passed |
| PBR → Oslo dependency handoff and negative test | Passed |
| Noble/UCA Epoxy backport | Passed |
| Glance UCA service package and API smoke | Passed |

Downloaded hosted evidence is retained under `artifacts/github/35498752368/`. `artifact-index.json` records GitHub's ZIP digests; `verification.json` records independent checks of downloaded source artifacts, binary checksums/metadata, installed versions, snapshot hashes, and exact producer dependency versions. Each GitHub artifact ZIP contains a `.tar.gz`; unpack that tar to access the original generation layout and sbuild filenames.

## 5. Glance service package

The pinned UCA source is `glance 2:30.0.0-0ubuntu1~cloud1.1`. Real builds produced `glance`, `glance-api`, `glance-common`, `python-glance-doc` and `python3-glance`. The packaging unit suite reports 2,242 tests with one skip. The final local fresh-install smoke passed, including database migration, version discovery and image create/upload/download/delete. All five installed Debian versions match the built version.

Evidence: `artifacts/validated/gen-8c81485e1605-638a15b6/`, including `smoke-install.json` with the API log and successful image round trip.

The service smoke test installs the actual binaries in a fresh chroot, checks every Debian version, runs database migrations, launches the installed API on loopback, and exercises image creation, upload, download and deletion. It uses a temporary SQLite database, filesystem store and explicit project-scoped test identity through the shipped request-context middleware. Policy checks remain active; this does not validate Keystone authentication or a deployed cloud.

## Agent validation

The final implementation has **78 passing agent tests**. Coverage includes real Debian artifact inspection, missing/corrupt outputs, dependency blocking and version enforcement, snapshot metadata and pin validation, first-parent cutoff selection, long Git histories, output-lock preservation, portable archive modes, UCA profile/revision rejection, process-start failures and timeouts.

## Remaining boundaries

Gump integration, additional UCA series, full dependency graph discovery, immutable archive dependency snapshots, service upgrade/autopkgtest coverage, and reviewable agent-generated packaging patches remain follow-up work. Source signing, package uploads and archive promotion are separate delivery features. No packages were uploaded or signed.
