# Validate a producer/consumer packaging chain

The Stonking integration case builds `python-pbr` first, then builds the pinned `python-oslo.i18n` snapshot against its generated binaries. The pbr Debian version is deliberately unique:

```
7.1.0+git20260813.1.9ec6e72-0ubuntu1+packagetest1
```

This prevents the archive's `-0ubuntu1` copy from passing the dependency assertion. The test uses the existing published upstream source with a local Debian revision; it does not attempt an unrelated pbr upstream upgrade.

## Run it

Inside the prepared Ubuntu builder VM:

```bash
bash scripts/prepare-builder.sh stonking
bash scripts/run-library.sh dependency
sg sbuild -c '.venv/bin/python scripts/test-dependency-failure.py config/locks/pbr-oslo-i18n-stonking.json'
```

The wrapper uses the exact generation returned by the build for installation checks. The negative test keeps its outputs in `artifacts/dependency-negative/` and reports success only when the intentional producer failure blocks the consumer.

## Guarantees checked

- Only producer binaries that passed artifact validation and Lintian are eligible for handoff.
- The executor rechecks their hashes immediately before passing them to sbuild with `--extra-package`.
- The consumer's `required_build_versions` adds an exact `--add-depends=python3-pbr (= VERSION)` solver constraint.
- The consumer's `.buildinfo` must independently report that exact installed version.
- The consumer manifest records producer filenames, versions, and hashes under `dependency_artifacts`.
- A fresh installation session installs all four generated binary packages and checks each installed Debian version, Oslo translation, and the full Python snapshot version.
- An intentionally corrupted producer tarball checksum causes `FAILED` for pbr and `BLOCKED` for oslo.i18n, with `blocked_by: ["python-pbr"]`. The consumer is not checked out or built.

The pinned PBR wheel used to generate the consumer's source sdist is a separate bootstrap tool. The newly built Debian pbr is used for the consumer's isolated **binary build**, which is the dependency edge this test proves.

## Evidence

The successful chain is `artifacts/validated/gen-07019ac10c56-d323c648/`. The consumer build log shows pbr fetched from sbuild's local `file:` repository and installed with the unique version above. Its `.buildinfo` and manifest record the same version. The host transcript is `artifacts/direct-dependency-chain.log`.

The negative case is `artifacts/dependency-negative/`, including `assertions.json`, a deliberately invalid lock, logs, and the failed generation manifest. This is an expected failure fixture, not a release input.

The 75 oslo.i18n upstream tests passed. Ubuntu's pinned pbr packaging rules intentionally skip pbr build-time unit tests when building on Ubuntu; those rules were preserved, so this is not a claim that pbr's full upstream test suite ran. Lintian returned no errors, with documentation/filename warnings retained in logs. Archive dependencies remain recorded rather than snapshot pinned.
