#!/usr/bin/env python3
"""Exercise the pinned local model, response parser, and source patch validator."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from packagetest.failure_analysis import (
    parse_repair_decision,
    render_source_repair,
    validate_source_patch,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--llama-cli", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    prompt = """Return only one JSON object with exactly these string keys: action, package, argument, evidence.
A Debian package build failed with:
ModuleNotFoundError: No module named 'futurist'
Choose add_dependency with the corresponding Debian Python 3 package, or no_fix if unjustified.
Use an empty argument and quote the error in evidence."""
    command = [str(args.llama_cli), "-m", str(args.model), "-p", prompt, "-n", "512", "-c", "4096",
               "--temp", "0", "--seed", "1", "--threads", "2", "--no-display-prompt",
               "--single-turn", "--simple-io", "--no-show-timings"]
    env = dict(os.environ)
    runtime = str(args.llama_cli.resolve().parent)
    env["LD_LIBRARY_PATH"] = runtime + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    completed = subprocess.run(command, text=True, capture_output=True, timeout=300, env=env)
    (args.output / "stdout.txt").write_text(completed.stdout)
    (args.output / "stderr.txt").write_text(completed.stderr)
    decision = parse_repair_decision(completed.stdout)
    (args.output / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
    patch = render_source_repair(decision, args.tree)
    (args.output / "proposal.patch").write_text(patch)
    validation = validate_source_patch(patch, args.tree)
    if decision["action"] != "add_dependency" or decision["package"] != "python3-futurist":
        validation = {**validation, "result": "REJECTED", "error": "model chose the wrong dependency repair"}
    result = {"returncode": completed.returncode, "decision": decision, "validation": validation}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if completed.returncode == 0 and validation["result"] == "APPLIES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
