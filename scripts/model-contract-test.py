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


CONTRACT_CASES = [
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


def _accepted_patch(decision: dict, validation: dict, tree: Path) -> str:
    """Render only an accepted mutation; abstention never counts as a patch."""
    if validation["result"] == "ACCEPTED" and decision["action"] != "no_fix":
        return render_source_repair(decision, tree)

    return ""


def _case_passes(returncode: int, decision: dict, case: dict,
                 decision_validation: dict, patch_validation: dict) -> bool:
    """Require successful inference, expected intent, and both independent validators."""
    chosen = {key: decision[key] for key in ("action", "subject", "replacement")}
    return (returncode == 0 and chosen == case["expected"] and
            decision_validation["result"] == "ACCEPTED" and patch_validation["result"] == "APPLIES")


def _evaluate_response(completed: subprocess.CompletedProcess[str], tree: Path,
                       case: dict, case_dir: Path) -> dict:
    """Validate normalized intent and the rendered patch against the source fixture."""
    raw_decision = parse_repair_decision(completed.stdout)
    decision = normalize_repair_decision(raw_decision, tree, case["evidence"])
    if decision != raw_decision:
        (case_dir / "raw-decision.json").write_text(json.dumps(raw_decision, indent=2) + "\n")

    (case_dir / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
    decision_validation = validate_repair_decision(decision, case["evidence"])
    patch = _accepted_patch(decision, decision_validation, tree)
    (case_dir / "proposal.patch").write_text(patch)
    patch_validation = validate_source_patch(patch, tree) if patch else {"result": "REJECTED"}
    passed = _case_passes(completed.returncode, decision, case, decision_validation, patch_validation)
    result = {"name": case["name"], "returncode": completed.returncode, "decision": decision,
              "decision_validation": decision_validation, "patch_validation": patch_validation,
              "result": "PASS" if passed else "FAIL"}
    return result


def _run_case(args: argparse.Namespace, env: dict[str, str], number: int, case: dict) -> dict:
    """Persist inference output and the contract verdict for one fixed case."""
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
        result = _evaluate_response(completed, args.tree, case, case_dir)
    except Exception as exc:
        result = {"name": case["name"], "returncode": completed.returncode,
                  "result": "FAIL", "error": str(exc)}

    (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _runtime_environment(executable: Path) -> dict[str, str]:
    """Make bundled runtime libraries available alongside inherited library paths."""
    env = dict(os.environ)
    runtime = str(executable.resolve().parent)
    env["LD_LIBRARY_PATH"] = runtime + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def main() -> int:
    """Exercise the pinned model against all fixed repair contracts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--llama-cli", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = _runtime_environment(args.llama_cli)
    results = []
    for number, case in enumerate(CONTRACT_CASES, 1):
        results.append(_run_case(args, env, number, case))

    result = {"result": "PASS" if all(case["result"] == "PASS" for case in results) else "FAIL",
              "cases": results}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
