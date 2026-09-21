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
    validate_repair_decision,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--llama-cli", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    runtime = str(args.llama_cli.resolve().parent)
    env["LD_LIBRARY_PATH"] = runtime + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    cases = [
        {
            "name": "missing-dependency",
            "evidence": "ModuleNotFoundError: No module named 'futurist'",
            "expected": {"action": "add_dependency", "package": "python3-futurist", "argument": ""},
        },
        {
            "name": "rejected-rule-argument",
            "evidence": "oslopolicy-sample-generator: error: unrecognized arguments: --format yaml",
            "expected": {"action": "remove_rule_argument", "package": "", "argument": "--format yaml"},
        },
    ]
    results = []
    for number, case in enumerate(cases, 1):
        case_dir = args.output / case["name"]
        case_dir.mkdir()
        prompt = f"""Return only one JSON object with exactly these string keys: action, package, argument, evidence.
Use add_dependency only for an exact ModuleNotFoundError and set package to the corresponding Debian python3-* package.
Use remove_rule_argument for an exact 'error: unrecognized arguments:' failure and set argument to the rejected
option exactly as it appears in debian/rules. This rule has priority over any command usage text.
Use no_fix if neither action is justified. Use an empty string for the field not used by the selected action.
A Debian package build failed with:
{case['evidence']}
Quote that exact failure in evidence."""
        command = [str(args.llama_cli), "-m", str(args.model), "-p", prompt, "-n", "512", "-c", "4096",
                   "--temp", "0", "--seed", str(number), "--threads", "2", "--no-display-prompt",
                   "--single-turn", "--simple-io", "--no-show-timings"]
        completed = subprocess.run(command, text=True, capture_output=True, timeout=300, env=env)
        (case_dir / "stdout.txt").write_text(completed.stdout)
        (case_dir / "stderr.txt").write_text(completed.stderr)
        decision = parse_repair_decision(completed.stdout)
        (case_dir / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
        decision_validation = validate_repair_decision(decision, case["evidence"])
        patch = "" if decision["action"] == "no_fix" else render_source_repair(decision, args.tree)
        (case_dir / "proposal.patch").write_text(patch)
        patch_validation = validate_source_patch(patch, args.tree) if patch else {"result": "REJECTED"}
        chosen = {key: decision[key] for key in ("action", "package", "argument")}
        passed = (completed.returncode == 0 and chosen == case["expected"] and
                  decision_validation["result"] == "ACCEPTED" and patch_validation["result"] == "APPLIES")
        result = {"name": case["name"], "returncode": completed.returncode, "decision": decision,
                  "decision_validation": decision_validation, "patch_validation": patch_validation,
                  "result": "PASS" if passed else "FAIL"}
        (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        results.append(result)
    result = {"result": "PASS" if all(case["result"] == "PASS" for case in results) else "FAIL",
              "cases": results}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
