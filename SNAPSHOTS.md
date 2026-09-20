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

The executor verifies Git ancestry and version inputs, checks generated metadata, normalizes tar/gzip headers, and requires the resulting orig tarball to match:

```
e299aa59689ec199a629793c7737264ebe0f0aa5d8bbd71ebcefab10f01feceb
```

The tarball was generated independently during the source probe and the full build with the same checksum. It is imported with pristine-tar; the binary build then runs in an isolated schroot. Installation checks both the Debian package version and the Python distribution version.

Ubuntu's published `upstream-hibiscus` branch was behind its packaging master. The lock therefore pins `03dd1aafd6be6e752a7a3e6299aaee995a3b70db`, the actual 6.9.0 upstream import already merged into the pinned packaging commit. Using the stale branch tip caused merge conflicts during the initial probe; those conflicts were not force-resolved.

## Evidence and limitations

The retained successful generation is `artifacts/validated/gen-e018f791b30e-08323b0e/`, containing source and binary artifacts, a generation manifest, full logs, and `smoke-install.json`. The host transcript is `artifacts/direct-snapshot.log`. Artifact metadata and checksums were verified again after copying them back from the VM.

Lintian completed without errors. Warnings include bundled JavaScript, long snapshot filenames, and Google Analytics references in generated HTML documentation. No test-skipping or packaging patches were introduced to obtain the successful build.

The agent's 61 unit tests pass, including snapshot version ordering, archive normalization, metadata rejection, and Git pin validation. The snapshot was validated on Stonking; Noble and UCA compatibility of this latest snapshot have not been tested. Archive dependencies are recorded but not snapshot pinned, so the full binary build is not claimed to be byte reproducible.

To select a newer snapshot, prepare a new reviewed lock: resolve and pin the new commit and tag ancestry, derive its version from the commit timestamp/count, check the current packaging import base, and generate/review its new sdist digest. Automatic rolling lock refresh is not yet implemented.
