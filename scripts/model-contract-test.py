#!/usr/bin/env python3
"""Exercise the pinned local model, response parser, and source patch validator."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from packagetest.failure_analysis import parse_model_response, validate_source_patch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--llama-cli", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    prompt = """Return a minimal git unified diff inside BEGIN_PATCH and END_PATCH.
The file debian/control currently contains:
Source: sample
Build-Depends: debhelper-compat (= 13)

Add python3-futurist to Build-Depends. Do not change anything else.
"""
    command = [str(args.llama_cli), "-m", str(args.model), "-p", prompt, "-n", "512", "-c", "4096",
               "--temp", "0", "--seed", "1", "--threads", "2", "--no-display-prompt",
               "--single-turn", "--simple-io", "--no-show-timings"]
    env = dict(os.environ)
    runtime = str(args.llama_cli.resolve().parent)
    env["LD_LIBRARY_PATH"] = runtime + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    completed = subprocess.run(command, text=True, capture_output=True, timeout=300, env=env)
    (args.output / "stdout.txt").write_text(completed.stdout)
    (args.output / "stderr.txt").write_text(completed.stderr)
    parsed = parse_model_response(completed.stdout)
    (args.output / "proposal.patch").write_text(parsed.patch)
    validation = validate_source_patch(parsed.patch, args.tree) if parsed.patch else {
        "result": "NO_PATCH", "paths": [], "error": "real model output contained no recoverable patch"
    }
    if validation["result"] == "APPLIES" and (
        validation["paths"] != ["debian/control"] or "python3-futurist" not in parsed.patch
    ):
        validation = {**validation, "result": "REJECTED",
                      "error": "model patch did not make the requested minimal dependency change"}
    result = {"returncode": completed.returncode, "validation": validation}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if completed.returncode == 0 and validation["result"] == "APPLIES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
