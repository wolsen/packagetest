# OpenStack packaging executor

Build and test pinned OpenStack Debian source packages in disposable Ubuntu
builders. The executor verifies source and binary checksums, Debian metadata,
Lintian results, and installed build dependencies. Nightly workflows prepare
snapshot sources, schedule candidate dependencies, run autopkgtest, and retain
bounded remediation attempts and reviewable packaging proposals.

## Start with a pinned build

Use a disposable Ubuntu 24.04 VM with passwordless sudo, at least 4 CPUs,
8 GiB RAM, and 40 GiB disk. Provisioning changes the builder's package and
schroot configuration.

```bash
bash scripts/prepare-builder.sh
bash scripts/run-library.sh baseline
```

This rebuilds the pinned Noble oslo.i18n package and checks installation in a
fresh schroot. See the [validation guide](docs/validation.md) for other cases.

In an already prepared builder:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-build-isolation -e .
.venv/bin/packaging build --plan config/locks/oslo-i18n-noble-baseline.json --dry-run
sg sbuild -c '.venv/bin/packaging build --plan config/locks/oslo-i18n-noble-baseline.json'
```

A dry run reports `PLANNED`, never successful execution. `packaging plan` is a
separate advisory release-discovery command; its output is not an executable lock.

## Documentation

- [Architecture](DESIGN.md): ownership, pipeline flow, trust boundaries, and limitations.
- [Pipeline contracts](docs/contracts.md): JSON identities, dependency rules, and result semantics.
- [Nightly operations](HIBISCUS-PIPELINE.md): target workflows, artifacts, and remediation.
- [Snapshot locks](SNAPSHOTS.md) and [Noble/UCA Epoxy](UCA.md): task-specific instructions.
- [Validation](docs/validation.md): test layers and integration acceptance criteria.
- [Roadmap](ROADMAP.md): remaining work.

## Development

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src scripts tests
.venv/bin/ruff format --check src scripts
.venv/bin/python -m mypy
.venv/bin/python scripts/check-complexity.py
.venv/bin/python scripts/check-docstrings.py
.venv/bin/bandit -r src scripts -lll -q
.venv/bin/pip-audit .
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-installed-package.sh
```

Some component tests require Debian tools such as quilt; CI installs them in a
separate job. The dependency audit needs network access. Every production
function has a complexity limit of 5; the exception baseline is empty. CI rejects
new exceptions unless the reviewed baseline is explicitly changed.

Ruff checks correctness, bug patterns, modern Python syntax, and import ordering
across source, scripts, and tests. Formatting covers source and scripts.
`check-docstrings.py` requires purpose docstrings for every production module,
class, function, and method, including private helpers, constructors, and nested
functions. Tests are exempt from docstring enforcement.

Strict mypy checks cover the 27 modules listed in `pyproject.toml`, including
prepared-source manifests, both dependency resolvers, final planning, nightly
build coordination, verified producer handoff, snapshot construction, and command
capture, bounded model repair plans, and repair admission policy. JSON enters
these paths as `object` and is validated before use. Other modules are still outside the strict gate; importing them does not imply that
mypy checks their implementation. Add modules to the explicit list as they are
converted, and keep `strict`, unreachable-code warnings, and missing-import type
checks enabled. See [the contract reference](docs/contracts.md#typed-metadata-boundaries)
for the serialized records.

Bandit gates high-severity findings; lower-severity findings require separate
review. Static checks complement the unit, Debian component, and installed-wheel
tests; they do not establish successful sbuild or autopkgtest execution.

Python application logic belongs in `src/packagetest`. Existing Python scripts
are compatibility adapters where migrated; shell scripts retain builder/CI
integration. Installed wheels include default planner configuration. Building
packages still requires the documented external tools and prepared environment.

Source pins establish input integrity, not bit-for-bit reproducibility: archive
dependencies are recorded but not snapshot-pinned. Signing, archive uploads,
and regress-stack are not implemented.
