# Build the Noble / UCA Epoxy cases

Use a disposable Ubuntu 24.04 VM with passwordless sudo. The profile creates a fresh file-backed schroot for each build and installation test:

```bash
bash scripts/prepare-builder.sh noble-uca-epoxy
bash scripts/run-library.sh uca
bash scripts/run-library.sh glance
```

The `uca` case builds `python-oslo.i18n` from the pinned Launchpad `stable/2025.1` packaging branch and `upstream-epoxy` import. The `glance` case rebuilds the published UCA source package `2:30.0.0-0ubuntu1~cloud1.1`. These are separate validation cases; Glance currently resolves its library dependencies from UCA, not from the local Oslo build.

## Target policy

| Setting | Value |
| --- | --- |
| Base OS | Ubuntu Noble 24.04, amd64 |
| OpenStack series | Epoxy, 2025.1 |
| Package distribution | `noble-epoxy` |
| Base pockets | `noble`, `noble-updates`, `noble-security` |
| Cloud pocket | `noble-updates/epoxy`, component `main` |
| Cloud mirror | `http://ubuntu-cloud.archive.canonical.com/ubuntu` |
| APT trust | Ubuntu's `ubuntu-cloud-keyring` package, explicit `signed-by` |
| Chroot | `noble-uca-epoxy-amd64-sbuild` |

Lintian's installed Ubuntu distribution data is extended with the single supported `noble-epoxy` name for these cases. The extended data and its hash are retained with the generation; no Lintian tags are disabled. The executor also requires the binary `.changes` distribution to match the target exactly.

The target follows the [Ubuntu Cloud Archive documentation](https://wiki.ubuntu.com/OpenStack/CloudArchive) and its [Epoxy package report](https://openstack-ci-reports.ubuntu.com/reports/cloud-archive/epoxy_versions.html). Provisioning leaves APT signature verification enabled. The executor checks the configured UCA repository and records effective sources plus resolved build dependency versions.

## Backport revision policy

The pinned packaging branch has `6.5.1-0ubuntu1.1` in its changelog. This case builds:

```text
6.5.1-0ubuntu1.1~cloud0+packagetest1
```

The lock records both `backport_of` and `previous_target_version`. Validation requires the exact stable branches, preserves the epoch, requires the `~cloudN` suffix, and ensures the new version sorts above the pinned prior UCA version (`6.5.1-0ubuntu1~cloud0`). It deliberately sorts below its Ubuntu packaging base, as a backport should. That exception to ordinary version advancement applies only to the explicit UCA profile and a matching pinned changelog base.

An archive baseline lock, `config/locks/oslo-i18n-noble-uca-epoxy-baseline.json`, is also available through `packaging build --plan`. It preserves the published version exactly. Source URLs and SHA256 hashes come from the UCA source index; the executor verifies those hashes rather than independently verifying `.dsc` signatures.

## Glance validation

The normal Debian build executes the package's unit tests, builds all five binaries, checks `.changes` and `.buildinfo`, and runs Lintian with errors fatal. The separate smoke test installs the exact built packages in a fresh chroot, checks their versions, migrates a temporary SQLite database, starts the installed API on a temporary loopback port, discovers its API versions, and creates, uploads, downloads, and deletes an image.

The smoke service uses Glance's shipped default unauthenticated pipeline and a temporary filesystem image store. It is a package installation and API check, not validation of a deployed OpenStack cloud, Keystone integration, database upgrades, or all autopkgtests. The API process and schroot session are cleaned up on failure as well as success.

Source archives, binaries, logs, the packaging diff for the library backport, and `smoke-install.json` are retained under each generation. No signing or archive upload is performed.
