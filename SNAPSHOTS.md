# Build a pinned oslo.i18n snapshot

The snapshot selected on 2026-09-20 is upstream master commit `7045af07a0654247ad9f3dfd416622afed3e1489`, dated 2026-09-14, three commits after tag 6.9.0. It successfully built and installed on **Ubuntu Stonking (26.10 development), amd64**, with Python 3.14. All 75 upstream tests passed.

The Debian version is:

```
6.9.0+git20260914.3.7045af0-0ubuntu1~packagetest1
```

The installed Python distribution reports `6.9.0+git20260914.3.7045af0`. The `~packagetest1` Debian revision denotes this local validation build.

## Repeat the test

In the prepared Ubuntu 24.04 builder VM, with the updated project checkout:

```bash
bash scripts/prepare-builder.sh stonking
bash scripts/run-library.sh snapshot
```

Or run the executor directly after provisioning:

```bash
sg sbuild -c '.venv/bin/packaging build --plan config/locks/oslo-i18n-stonking-snapshot.json'
.venv/bin/python scripts/smoke-install.py artifacts
```

This repeats the checked-in snapshot. It deliberately does not resolve a new master commit on each invocation.

## What is pinned and checked

The lock pins the full upstream SHA, base tag and its commit, commit date, distance from the base tag, Ubuntu packaging commit, upstream import base, pristine-tar commit, and checksum-verified PBR/setuptools/wheel versions. A full-history checkout generates AUTHORS, ChangeLog, and PKG-INFO through PBR's source-distribution command.

The executor verifies Git ancestry and version inputs, checks generated metadata, normalizes tar/gzip headers, and originally required the following orig tarball checksum (see the portable format update below):

```
e299aa59689ec199a629793c7737264ebe0f0aa5d8bbd71ebcefab10f01feceb
```

The tarball was generated independently during the source probe and the full build with the same checksum. It is imported with pristine-tar; the binary build then runs in an isolated schroot. Installation checks both the Debian package version and the Python distribution version.

Ubuntu's published `upstream-hibiscus` branch was behind its packaging master. The lock therefore pins `03dd1aafd6be6e752a7a3e6299aaee995a3b70db`, the actual 6.9.0 upstream import already merged into the pinned packaging commit. Using the stale branch tip caused merge conflicts during the initial probe; those conflicts were not force-resolved.

## Evidence and limitations

The retained successful generation is `artifacts/validated/gen-e018f791b30e-08323b0e/`, containing source and binary artifacts, a generation manifest, full logs, and `smoke-install.json`. The host transcript is `artifacts/direct-snapshot.log`. Artifact metadata and checksums were verified again after copying them back from the VM.

Lintian completed without errors. Warnings include bundled JavaScript, long snapshot filenames, and Google Analytics references in generated HTML documentation. No test-skipping or packaging patches were introduced to obtain the successful build.

The original snapshot checkpoint had 61 passing agent unit tests, including snapshot version ordering, archive normalization, metadata rejection, and Git pin validation. The snapshot was validated on Stonking; Noble and UCA compatibility of this latest snapshot have not been tested. Archive dependencies are recorded but not snapshot pinned, so the full binary build is not claimed to be byte reproducible.

## Create a new lock from a branch or timestamp

In the prepared builder, run discovery explicitly:

```bash
.venv/bin/packaging lock-snapshot \
  --template config/locks/oslo-i18n-stonking-snapshot.json \
  --ref master --output artifacts/oslo-latest.json

.venv/bin/packaging lock-snapshot \
  --template config/locks/oslo-i18n-stonking-snapshot.json \
  --ref master --at 2026-09-10T00:00:00Z \
  --output artifacts/oslo-cutoff.json
```

Review the output lock, then pass it to `packaging build --plan`. Discovery preserves the template's packaging commits, target and pinned build tools, resolves the upstream commit and release-tag ancestry, derives the full version, generates the sdist and writes its digest. It refuses to overwrite an existing lock and writes no output lock when source generation fails. Only single-package snapshot templates are supported initially.

A cutoff selects the first commit on the branch's **first-parent history**, walking backwards from its currently resolved tip, whose committer timestamp is at or before the cutoff. A timezone is mandatory. This describes current Git history as of a commit timestamp; it cannot reconstruct a branch's historical server-side ref after a force push. A cutoff exactly at a release tag requires a release lock instead. The September 10 example selects `b3b3686517d5da714a46f4a13684f9376535e225`, yielding `6.9.0+git20260903.1.b3b3686`.

Builds consume the written lock without resolving the branch again. Creating a new lock is a separate, explicit action; a scheduled rolling refresh is not configured.

## Portable source archive format

Hosted CI exposed a difference in the original archive recipe: the VM used umask 0002 and GitHub used 0022. Contents matched, but tar file modes differed. New locks use `archive_format: portable-v1`, normalizing directories and executable files to 0755 and other files to 0644, in addition to owner, timestamp, ordering and gzip normalization.

The current checked-in snapshot's digest is:

```text
de75efaa73e20b6db3f918b9e4641d944a4b8a6b9246edb207fbfa3236ec55b9
```

The earlier digest and generation above record the original local validation. The revised recipe was applied independently to the local and hosted archives and produced identical bytes. Locks without `archive_format` retain the legacy recipe; their reviewed checksums are not silently reinterpreted. Full Debian binary reproducibility still requires further work on archive dependencies and generated packaging metadata.
