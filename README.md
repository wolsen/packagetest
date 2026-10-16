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
.venv/bin/ruff format --check src/packagetest/contracts.py src/packagetest/relations.py
.venv/bin/python -m mypy
.venv/bin/python scripts/check-complexity.py
.venv/bin/bandit -r src scripts -lll -q
.venv/bin/pip-audit .
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-installed-package.sh
```

Some component tests require Debian tools such as quilt; CI installs them in a
separate job. The dependency audit needs network access. Complexity checks cap
new functions at 5 and reject increases above recorded legacy exceptions.
When reducing complexity, lower the corresponding baseline entry. Baseline
changes require review; CI never refreshes them automatically. Formatting and
strict type checks initially cover the new typed boundaries, while correctness
linting covers the whole Python tree. Bandit currently gates high-severity
findings; it is not a claim that lower-severity findings have all been resolved.

Python application logic belongs in `src/packagetest`. Existing Python scripts
are compatibility adapters where migrated; shell scripts retain builder/CI
integration. Installed wheels include default planner configuration. Building
packages still requires the documented external tools and prepared environment.

Source pins establish input integrity, not bit-for-bit reproducibility: archive
dependencies are recorded but not snapshot-pinned. Signing, archive uploads,
and regress-stack are not implemented.
