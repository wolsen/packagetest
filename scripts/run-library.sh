#!/usr/bin/env bash
# Run in an already provisioned Ubuntu 24.04 builder, locally or in CI.
set -euo pipefail
build_case=${1:-baseline}
case "$build_case" in
    baseline|update) lock="config/locks/oslo-i18n-noble-$build_case.json" ;;
    snapshot) lock=config/locks/oslo-i18n-stonking-snapshot.json ;;
    dependency) lock=config/locks/pbr-oslo-i18n-stonking.json ;;
    uca) lock=config/locks/oslo-i18n-noble-uca-epoxy.json ;;
    glance) lock=config/locks/glance-noble-uca-epoxy.json ;;
    resolved-snapshot) lock=artifacts/resolved-oslo-latest.json ;;
    *) echo 'Expected baseline, update, snapshot, dependency, uca, glance, or resolved-snapshot' >&2; exit 2 ;;
esac
# Serialize shared editable-environment setup when local cases run concurrently.
mkdir -p artifacts
(
    flock 9
    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install --no-build-isolation -e .
) 9>artifacts/environment.lock
.venv/bin/python -m pytest -q
if [[ "$build_case" == resolved-snapshot ]]; then
    .venv/bin/packaging lock-snapshot --template config/locks/oslo-i18n-stonking-snapshot.json --output "$lock"
fi
result=$(sg sbuild -c ".venv/bin/packaging build --plan $lock")
printf '%s\n' "$result"
manifest=$(.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["run_dir"] + "/generation-manifest.json")' <<< "$result")
.venv/bin/python scripts/smoke-install.py "$manifest"
