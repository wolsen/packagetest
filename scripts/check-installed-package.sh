#!/usr/bin/env bash
# Build and exercise an installed wheel outside the source checkout, without network.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
python3 -m build --wheel --no-isolation --outdir "$work/dist" "$root"
python3 -m venv --system-site-packages "$work/venv"
"$work/venv/bin/python" -m pip install --ignore-installed --no-deps "$work"/dist/*.whl
cd "$work"
unset PYTHONPATH
"$work/venv/bin/packaging" --help
"$work/venv/bin/python" -m packagetest.nightly_plan --help
"$work/venv/bin/python" -m packagetest.final_plan --help
"$work/venv/bin/python" - <<'PY'
from pathlib import Path
import sys
import packagetest
assert Path(packagetest.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
from packagetest.config import default_config_path, load_package_definitions
import json
assert load_package_definitions(default_config_path('vertical_slice.json'))
assert isinstance(json.loads(default_config_path('hibiscus-candidate-dependencies.json').read_text()), list)
PY
