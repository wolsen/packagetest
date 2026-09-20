# Hibiscus snapshot validation — 20 September 2026

The pipeline targets 197 Ubuntu source packages for OpenStack 2026.2 on Ubuntu
26.04 (Resolute), amd64. Builds run in Resolute sbuild chroots; autopkgtests use
Resolute QEMU guests on GitHub-hosted runners. Regress-stack remains deferred.

## Completed baseline run

[Run 35510687081](https://github.com/wolsen/packagetest/actions/runs/35510687081)
completed at commit `9ddf1a0`. Its structured results account for every requested
source; there are no missing status reports or autopkgtest infrastructure errors.

| Build result | Sources |
|---|---:|
| Succeeded | 104 |
| Failed | 11 |
| Blocked by dependencies | 82 |

| Autopkgtest result | Sources |
|---|---:|
| PASS | 42 |
| SUPERFICIAL | 21 |
| NO_TESTS | 38 |
| FAIL | 3 |
| BLOCKED | 93 |

These counts come from the verified `pipeline-summary` artifact, not GitHub job
conclusions: a job that records a blocked test can itself finish successfully.
The downloaded ZIP SHA-256 is
`71ea67a41268fc892f404b79a2607ce504cf39ee561ff98ebe79b7ba4ba33742`.
The complete package results are available in the run's artifacts. Local retained
evidence is under `artifacts/github/35510687081/summary/`.

## Repairs submitted for the next run

The repair batch through commit `eaa5ab8` addresses all eleven identified build
failures:

- Missing build dependencies in openstacksdk, zaqarclient, oslo.middleware and
  Freezer Client.
- Snapshot dependency versions and ordering for Manila Client, OpenStack Client,
  osc-lib and oslo.service.
- Missing generated distribution metadata during Ironic Client tests.
- A Lintian false positive on oslo.config's unrendered Bash template.
- A Rootwrap test fixture that assumed the resolved `cat` executable was on PATH.
  Production security checks and negative test assertions remain unchanged.

The three actual autopkgtest failures also have submitted fixes. Octavia's
standalone Go helper was incorrectly treated as a Go library by generated tests;
explicit tests now exercise its installed HTTP server and Tempest plugin
registration. Debtcollector and osprofiler smoke tests now use the supported
distribution-metadata API instead of deprecated version attributes. Their
existing superficial restrictions remain intact.

The planner now incorporates checksum-verified packaging control replacements.
It preserves mandatory candidate dependencies and reuses earlier-wave producers
within cycles. This keeps eight parallel build waves while reducing archive
bootstrap edges to 188. Exact version verification uses the generated source
package's build dependencies, including architecture-independent dependencies.

The submitted batch passed 173 repository tests, workflow validation, and fresh
source-package checks for repaired packaging. Selected failing tests were also
reproduced and checked locally. Full remote validation is still required:
[run 35514574466](https://github.com/wolsen/packagetest/actions/runs/35514574466)
tests the repaired catalog.

## Completed follow-up run

[Run 35514574466](https://github.com/wolsen/packagetest/actions/runs/35514574466)
completed at `eaa5ab8`. Its verified summary accounts for all 197 sources:

| Phase | Results |
|---|---|
| Builds | 120 succeeded, 4 failed, 73 blocked |
| Autopkgtests | 57 PASS, 21 SUPERFICIAL, 42 NO_TESTS, 77 BLOCKED |

There were no actual autopkgtest failures or infrastructure errors. This confirms
Octavia's two explicit tests and osprofiler's unit tests; debtcollector correctly
remains superficial. The ZIP SHA-256 is
`3e6c3b20c83b5d9965fdbb4b3b8490ff2f8fc18b15dc2c8cb3b4bc53ee3273f1`.
Retained results are under `artifacts/github/35514574466/summary/`.

The repair batch through `cc71d8e` addresses all four remaining direct failures:

- SDK requires candidate keystoneauth1 >=5.16.0. Its catalog test accepts newer
  snapshot catalogs while rejecting stale or malformed dates.
- oslo.versionedobjects requires mypy >=1.19.0; type assertions normalize the
  supported version's builtin-name display. All 57 plugin tests pass locally.
- Ironic Agent's setuptools configuration omitted runtime subpackages. The rebuilt
  wheel contains all 21 entry-point modules; installed metrics imports pass.
- CloudKitty requires observabilityclient >=1.1.0 for builds and runtime.

The batch passes 177 repository tests, workflow validation, and fresh source
checks. Its full remote binary validation remains pending. The graph retains
197 sources across eight parallel waves. A separate audit verified runtime files
and imports in actual cliff, oslo.messaging, and oslo.service candidate binaries;
source configuration alone did not justify broader package discovery changes.

## Remaining release gates

Missing and superficial tests do not count as substantive passes. Package tests
also vary in depth: an import check or service-start check does not establish
that a complete OpenStack deployment works. The remaining cycle bootstrap edges
still use archive dependencies; a complete candidate dependency rebuild and
retest are needed before claiming a closed release candidate.

The nightly schedule is prepared for 09:00 UTC (02:00 America/Phoenix), but it is
not active on this feature branch. Default-branch integration remains pending.
The full regress-stack gate has not been run.
