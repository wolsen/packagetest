# Validate packaging changes

Run Python tests and quality checks using the commands in [README](../README.md).
Use a disposable prepared Ubuntu builder for the integration cases below.

| Case | Command | Required outcome |
| --- | --- | --- |
| Noble baseline | `bash scripts/run-library.sh baseline` | Verified binaries and fresh-install translation |
| Pinned upstream update | `bash scripts/run-library.sh update` | gbp preparation, verified binaries, fresh install |
| Snapshot | `bash scripts/run-library.sh snapshot` | Pinned sdist and installed Python version match |
| Candidate handoff | `bash scripts/run-library.sh dependency` | Consumer uses exact producer version |
| UCA Epoxy | `bash scripts/run-library.sh uca` | Correct backport version and installation |
| Glance | `bash scripts/run-library.sh glance` | Installation and API image round trip |
| Nightly targets | Dispatch the target workflow | Prepared inputs, graph, build/test readiness, report |

Provision each case's target first; see [snapshot instructions](../SNAPSHOTS.md)
and [UCA instructions](../UCA.md). The dependency failure check in CI deliberately
corrupts the producer and requires the consumer to remain blocked.

## Check metadata and quality changes

Run strict typing and the formatting, lint, complexity, and docstring gates from
the README before committing. When expanding typed coverage, add the converted
module to `tool.mypy.files` rather than disabling strict checks or adding blanket
ignores. Validate JSON at its input boundary and use named records for established
schemas; retain `object` for extension fields until their consumers validate them.

Add regression cases when validation or nullable values reveal a behavior defect.
Exercise valid artifacts as well as rejected shapes so a stricter parser continues
to accept the exported schema. For source or producer metadata changes, run the
Debian component tests and installed-wheel smoke check too.

Keep a blank line between a completed conditional block and the following logical
step. Give every production definition a purpose docstring, including private
helpers and constructors. Use inline comments to explain constraints or policy
that the code alone does not make clear.

## Test layers

- Unit tests cover parsing, planning policy, identity validation, deadlines, and reporting.
- `packaging_component` tests exercise real Debian tools, including quilt.
- The installed-wheel smoke check runs outside the checkout and verifies shipped defaults.
- Builder regressions exercise sbuild and fresh installation.
- Nightly autopkgtest runs in separate guests. Accepted missing/superficial coverage
  is reported distinctly from substantive test passes.

For each integration run, record the commit, exact command, target, lock/catalog
identity, run ID and attempt, result artifacts, and any skipped gates in the run
artifacts. Acceptance applies to the tested revision and environment.
