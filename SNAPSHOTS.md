# Build a pinned oslo.i18n snapshot

The checked-in lock pins upstream master commit
`7045af07a0654247ad9f3dfd416622afed3e1489`, dated 2026-09-14, three commits
after tag 6.9.0. Its target is Ubuntu Stonking, amd64.

The Debian version is:

```
6.9.0+git20260914.3.7045af0-0ubuntu1~packagetest1
```

The installed Python distribution reports `6.9.0+git20260914.3.7045af0`. The `~packagetest1` Debian revision denotes this local validation build.

## Run the snapshot build

In the prepared Ubuntu 24.04 builder VM, with the project checkout:

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

The executor verifies Git ancestry and version inputs, checks generated metadata,
and normalizes tar/gzip headers. The tarball is imported with pristine-tar;
the binary build runs in an isolated schroot. Installation checks both the
Debian package version and the Python distribution version.

The upstream import commit must match the source already merged into the pinned
packaging commit. Packaging conflicts fail source preparation.

Archive dependencies are recorded but not snapshot pinned, so the full binary
build is not claimed to be byte reproducible. See the
[validation guide](docs/validation.md) for acceptance criteria.

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

Locks using `archive_format: portable-v1` normalize directories and executable
files to 0755 and other files to 0644, in addition to owner, timestamp, ordering,
and gzip normalization. This makes generated source archives independent of the
builder's umask.

The checked-in snapshot's digest is:

```text
de75efaa73e20b6db3f918b9e4641d944a4b8a6b9246edb207fbfa3236ec55b9
```

Locks without `archive_format` use the legacy recipe; their reviewed checksums
are not silently reinterpreted. Full Debian binary reproducibility also requires
control of archive dependencies and generated packaging metadata.
