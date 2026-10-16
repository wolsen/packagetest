#!/usr/bin/env python3
"""Exercise the pinned local model, response parser, and source patch validator."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from packagetest.failure_analysis import (
    normalize_repair_decision,
    parse_repair_decision,
    render_source_repair,
    validate_repair_decision,
    validate_source_patch,
)

CONTRACT_CASES = [
    {
        "name": "missing-dependency",
        "evidence": "ModuleNotFoundError: No module named 'futurist'",
        "expected": {
            "action": "add_dependency",
            "subject": "python3-futurist",
            "replacement": "",
        },
    },
    {
        "name": "rejected-rule-argument",
        "evidence": "oslopolicy-sample-generator: error: unrecognized arguments: --format yaml",
        "expected": {
            "action": "remove_rule_argument",
            "subject": "--format yaml",
            "replacement": "",
        },
    },
    {
        "name": "context-drifted-quilt-patch",
        "evidence": (
            "context-only.patch subprocess returned exit status 1; Hunk #1 FAILED. "
            "The old transformation remains applicable and only surrounding context changed."
        ),
        "expected": {
            "action": "refresh_quilt_patch",
            "subject": "context-only.patch",
            "replacement": "",
        },
    },
    {
        "name": "superseded-quilt-patch",
        "evidence": (
            "embedded-xstatic.patch subprocess returned exit status 1; Hunk #1 FAILED. "
            "The supplied current upstream target implements the patch's intended behavior."
        ),
        "expected": {
            "action": "drop_quilt_patch",
            "subject": "embedded-xstatic.patch",
            "replacement": "",
        },
    },
    {
        "name": "moved-upstream-config",
        "evidence": (
            "ConfigFilesNotFoundError: Failed to find some config files: "
            "aodh/cmd/aodh-config-generator.conf. Snapshot candidate: "
            "etc/aodh/aodh-config-generator.conf"
        ),
        "expected": {
            "action": "replace_packaging_path",
            "subject": "aodh/cmd/aodh-config-generator.conf",
            "replacement": "etc/aodh/aodh-config-generator.conf",
        },
    },
]


def _accepted_patch(decision: dict, validation: dict, tree: Path) -> str:
    """Render only an accepted mutation; abstention never counts as a patch."""
    if validation["result"] == "ACCEPTED" and decision["action"] != "no_fix":
        return render_source_repair(decision, tree)

    return ""


def _case_passes(
    returncode: int,
    decision: dict,
    case: dict,
    decision_validation: dict,
    patch_validation: dict,
) -> bool:
    """Require successful inference, expected intent, and both independent validators."""
    chosen = {key: decision[key] for key in ("action", "subject", "replacement")}
    return (
        returncode == 0
        and chosen == case["expected"]
        and decision_validation["result"] == "ACCEPTED"
        and patch_validation["result"] == "APPLIES"
    )


def _evaluate_response(
    completed: subprocess.CompletedProcess[str], tree: Path, case: dict, case_dir: Path
) -> dict:
    """Validate normalized intent and the rendered patch against the source fixture."""
    raw_decision = parse_repair_decision(completed.stdout)
    decision = normalize_repair_decision(raw_decision, tree, case["evidence"])
    if decision != raw_decision:
        (case_dir / "raw-decision.json").write_text(
            json.dumps(raw_decision, indent=2) + "\n"
        )

    (case_dir / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
    decision_validation = validate_repair_decision(decision, case["evidence"])
    patch = _accepted_patch(decision, decision_validation, tree)
    (case_dir / "proposal.patch").write_text(patch)
    patch_validation = (
        validate_source_patch(patch, tree) if patch else {"result": "REJECTED"}
    )
    passed = _case_passes(
        completed.returncode, decision, case, decision_validation, patch_validation
    )
    result = {
        "name": case["name"],
        "returncode": completed.returncode,
        "decision": decision,
        "decision_validation": decision_validation,
        "patch_validation": patch_validation,
        "result": "PASS" if passed else "FAIL",
    }
    return result


def _run_case(
    args: argparse.Namespace, env: dict[str, str], number: int, case: dict
) -> dict:
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
{case["evidence"]}
Quote that exact failure in evidence."""
    command = [
        str(args.llama_cli),
        "-m",
        str(args.model),
        "-p",
        prompt,
        "-n",
        "128",
        "-c",
        "4096",
        "--temp",
        "0",
        "--seed",
        str(number),
        "--threads",
        "2",
        "--no-display-prompt",
        "--single-turn",
        "--simple-io",
        "--no-show-timings",
    ]
    completed = subprocess.run(
        command, text=True, capture_output=True, timeout=300, env=env
    )
    (case_dir / "stdout.txt").write_text(completed.stdout)
    (case_dir / "stderr.txt").write_text(completed.stderr)
    try:
        result = _evaluate_response(completed, args.tree, case, case_dir)
    except Exception as exc:
        result = {
            "name": case["name"],
            "returncode": completed.returncode,
            "result": "FAIL",
            "error": str(exc),
        }

    (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _runtime_environment(executable: Path) -> dict[str, str]:
    """Make bundled runtime libraries available alongside inherited library paths."""
    env = dict(os.environ)
    runtime = str(executable.resolve().parent)
    env["LD_LIBRARY_PATH"] = runtime + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
    )
    return env


def _investigation_fixture(tree: Path) -> None:
    """Create an independent quilt source with an intentionally narrow version check."""
    (tree / "debian/source").mkdir(parents=True)
    (tree / "debian/source/format").write_text("3.0 (quilt)\n")
    (tree / "debian/control").write_text(
        "Source: sample\n\nPackage: sample\nArchitecture: all\nDescription: fixture\n model contract\n"
    )
    (tree / "debian/changelog").write_text(
        "sample (1.0-1) unstable; urgency=medium\n\n  * Contract fixture.\n\n"
        " -- Example <example@example.org>  Fri, 16 Oct 2026 12:00:00 +0000\n"
    )
    (tree / "compatibility.py").write_text(
        '"""Accept data at or above the documented minimum version."""\n\n'
        "MINIMUM = 2024\n\ndef supported(version):\n"
        '    """Report whether the supplied data meets the minimum."""\n'
        "    return version == MINIMUM\n"
    )


def _minimum_contract(tree: Path) -> bool:
    """Assess the fixture invariant statically without executing model-authored code on the host."""
    import ast

    syntax = ast.parse((tree / "compatibility.py").read_text())
    original = ast.parse("MINIMUM = 2024")
    baseline = syntax.body[1]
    function = syntax.body[2]
    expected = ast.parse(
        "def supported(version):\n    return version >= MINIMUM\n"
    ).body[0]
    return (
        ast.dump(baseline) == ast.dump(original.body[0])
        and isinstance(function, ast.FunctionDef)
        and ast.dump(function.body[-1]) == ast.dump(expected.body[0])
    )


def _agent_turn(
    args: argparse.Namespace,
    tree: Path,
    directory: Path,
    number: int,
    evidence: str,
    feedback: str,
) -> dict:
    """Use the production prompt and runtime while retaining every model turn."""
    from packagetest.remediation import llama_generate, prompt
    from packagetest.repair_agent import parse_response

    text = prompt(
        "sample",
        "build",
        evidence,
        "Source root contains compatibility.py and debian/.\n" + feedback,
    )
    (directory / "prompt.txt").write_text(text)
    output, metadata = llama_generate(args.llama_cli, args.model, text, number, 420)
    (directory / "model-output.txt").write_text(output)
    (directory / "inference.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return parse_response(output)


def _agent_proposal(decision: dict, tree: Path, directory: Path, evidence: str) -> bool:
    """Require evidence, policy, patch application, and the independent fixture invariant."""
    from packagetest.repair_agent import (
        PatchPlan,
        materialize_source,
        render_edit_repair,
    )

    PatchPlan.from_mapping(decision, evidence)
    patch = render_edit_repair(decision, tree)
    (directory / "proposal.patch").write_text(patch)
    validation = validate_source_patch(patch, tree, apply=True)
    (directory / "patch-validation.json").write_text(
        json.dumps(validation, indent=2) + "\n"
    )
    if validation["result"] != "APPLIED":
        raise ValueError(validation["error"])

    materialize_source(tree)
    return _minimum_contract(tree)


def _investigation_turn(
    args: argparse.Namespace,
    tree: Path,
    directory: Path,
    number: int,
    evidence: str,
    feedback: str,
) -> tuple[bool, str]:
    """Answer inspection or evaluate a final proposal, returning bounded retry feedback."""
    from packagetest.repair_agent import SourceInspector

    decision = _agent_turn(args, tree, directory, number, evidence, feedback)
    (directory / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
    if decision.get("action") == "inspect":
        result = SourceInspector(tree).inspect(decision)
        (directory / "inspection.json").write_text(result + "\n")
        return False, result

    if decision.get("action") != "propose_patch":
        raise ValueError(
            "This contract requires investigation and a general source edit"
        )

    return _agent_proposal(
        decision, tree, directory, evidence
    ), "Proposal did not restore the documented minimum-version invariant."


def _run_investigation_case(args: argparse.Namespace) -> dict:
    """Exercise a bounded real-model investigation independently of the mechanical contracts."""
    case_dir = args.output / "general-source-investigation"
    case_dir.mkdir()
    tree = case_dir / "source"
    _investigation_fixture(tree)
    evidence = "AssertionError: supported(2026) returned False in compatibility.py; data versions at or above MINIMUM must be accepted."
    feedback = "Inspect the implementation before proposing an exact edit."
    for number in range(1, 5):
        directory = case_dir / f"turn-{number}"
        directory.mkdir()
        try:
            passed, feedback = _investigation_turn(
                args, tree, directory, number, evidence, feedback
            )
            if passed and list(case_dir.glob("turn-*/inspection.json")):
                return {"name": case_dir.name, "result": "PASS", "model_calls": number}

        except Exception as exc:
            feedback = str(exc)
            (directory / "error.txt").write_text(feedback + "\n")

    return {
        "name": case_dir.name,
        "result": "FAIL",
        "model_calls": 4,
        "error": feedback,
    }


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

    results.append(_run_investigation_case(args))
    result = {
        "result": "PASS"
        if all(case["result"] == "PASS" for case in results)
        else "FAIL",
        "cases": results,
    }
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
