#!/usr/bin/env bash
# Run in an already provisioned Ubuntu 24.04 builder, locally or in CI.
set -euo pipefail
build_case=${1:-baseline}
case "$build_case" in
    baseline|update) lock="config/locks/oslo-i18n-noble-$build_case.json" ;;
    snapshot) lock=config/locks/oslo-i18n-stonking-snapshot.json ;;
    *) echo 'Expected baseline, update, or snapshot' >&2; exit 2 ;;
esac
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-build-isolation -e .
.venv/bin/python -m pytest -q
sg sbuild -c ".venv/bin/packaging build --plan $lock"
.venv/bin/python scripts/smoke-install.py artifacts
