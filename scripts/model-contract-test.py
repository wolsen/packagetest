#!/usr/bin/env python3
"""Exercise the pinned local model, response parser, and source patch validator."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from packagetest.failure_analysis import (
    normalize_repair_decision,
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
            "expected": {"action": "add_dependency", "subject": "python3-futurist", "replacement": ""},
        },
        {
            "name": "rejected-rule-argument",
            "evidence": "oslopolicy-sample-generator: error: unrecognized arguments: --format yaml",
            "expected": {"action": "remove_rule_argument", "subject": "--format yaml", "replacement": ""},
        },
        {
            "name": "context-drifted-quilt-patch",
            "evidence": ("context-only.patch subprocess returned exit status 1; Hunk #1 FAILED. "
                         "The old transformation remains applicable and only surrounding context changed."),
            "expected": {"action": "refresh_quilt_patch", "subject": "context-only.patch", "replacement": ""},
        },
        {
            "name": "superseded-quilt-patch",
            "evidence": ("embedded-xstatic.patch subprocess returned exit status 1; Hunk #1 FAILED. "
                         "The supplied current upstream target implements the patch's intended behavior."),
            "expected": {"action": "drop_quilt_patch", "subject": "embedded-xstatic.patch", "replacement": ""},
        },
        {
            "name": "moved-upstream-config",
            "evidence": ("ConfigFilesNotFoundError: Failed to find some config files: "
                         "aodh/cmd/aodh-config-generator.conf. Snapshot candidate: "
                         "etc/aodh/aodh-config-generator.conf"),
            "expected": {"action": "replace_packaging_path",
                         "subject": "aodh/cmd/aodh-config-generator.conf",
                         "replacement": "etc/aodh/aodh-config-generator.conf"},
        },
    ]
    results = []
    for number, case in enumerate(cases, 1):
        case_dir = args.output / case["name"]
        case_dir.mkdir()
        prompt = f"""Return only one JSON object with exactly these string keys: action, subject, replacement, evidence.
Use add_dependency only for an exact ModuleNotFoundError and set subject to the corresponding Debian python3-* package.
Use remove_rule_argument for an exact 'error: unrecognized arguments:' failure and set subject to the rejected
option exactly as it appears in debian/rules. This rule has priority over any command usage text.
Use refresh_quilt_patch when a named patch's old transformation remains applicable and only its context changed.
Set subject to the patch filename and replacement to empty.
Use drop_quilt_patch only when a named patch fails and the evidence says current upstream implements its purpose.
Set subject to the patch filename and replacement to empty.
Use replace_packaging_path when a packaging path is missing and an existing snapshot replacement is named.
Set subject to the missing path and replacement to the existing path.
Use no_fix if neither action is justified. Use an empty replacement when it is unused.
A Debian package build failed with:
{case['evidence']}
Quote that exact failure in evidence."""
        command = [str(args.llama_cli), "-m", str(args.model), "-p", prompt, "-n", "128", "-c", "4096",
                   "--temp", "0", "--seed", str(number), "--threads", "2", "--no-display-prompt",
                   "--single-turn", "--simple-io", "--no-show-timings"]
        completed = subprocess.run(command, text=True, capture_output=True, timeout=300, env=env)
        (case_dir / "stdout.txt").write_text(completed.stdout)
        (case_dir / "stderr.txt").write_text(completed.stderr)
        try:
            raw_decision = parse_repair_decision(completed.stdout)
            decision = normalize_repair_decision(raw_decision, args.tree)
            if decision != raw_decision:
                (case_dir / "raw-decision.json").write_text(json.dumps(raw_decision, indent=2) + "\n")
            (case_dir / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
            decision_validation = validate_repair_decision(decision, case["evidence"])
            patch = (render_source_repair(decision, args.tree)
                     if decision_validation["result"] == "ACCEPTED" and decision["action"] != "no_fix" else "")
            (case_dir / "proposal.patch").write_text(patch)
            patch_validation = validate_source_patch(patch, args.tree) if patch else {"result": "REJECTED"}
            chosen = {key: decision[key] for key in ("action", "subject", "replacement")}
            passed = (completed.returncode == 0 and chosen == case["expected"] and
                      decision_validation["result"] == "ACCEPTED" and patch_validation["result"] == "APPLIES")
            result = {"name": case["name"], "returncode": completed.returncode, "decision": decision,
                      "decision_validation": decision_validation, "patch_validation": patch_validation,
                      "result": "PASS" if passed else "FAIL"}
        except Exception as exc:
            result = {"name": case["name"], "returncode": completed.returncode,
                      "result": "FAIL", "error": str(exc)}
        (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        results.append(result)
    result = {"result": "PASS" if all(case["result"] == "PASS" for case in results) else "FAIL",
              "cases": results}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
