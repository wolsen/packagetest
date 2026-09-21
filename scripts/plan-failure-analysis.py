#!/usr/bin/env python3
"""Create a bounded matrix of direct failures for advisory local-AI analysis."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from packagetest.failure_analysis import select_direct_failures, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, default=Path("failure-analysis-plan"))
    parser.add_argument("--limit", type=int, default=24)
    args = parser.parse_args()
    rows = json.loads(args.results.read_text())
    failures = select_direct_failures(rows)[: args.limit]
    matrix = {"include": failures}
    write_json(args.output / "matrix.json", matrix)
    write_json(args.output / "failures.json", failures)
    summary = ["# Local AI failure-analysis plan", "", f"{len(failures)} direct failures selected; blocked dependents are excluded.", ""]
    if failures:
        summary.extend(["| Source | Phase | Result |", "|---|---|---|"])
        summary.extend(f"| `{row['source']}` | {row['phase']} | {row['result']} |" for row in failures)
    else:
        summary.append("No direct build or autopkgtest failures require analysis.")
    (args.output / "summary.md").write_text("\n".join(summary) + "\n")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as stream:
            stream.write(f"enabled={'true' if failures else 'false'}\n")
            stream.write("matrix=" + json.dumps(matrix, separators=(",", ":")) + "\n")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as stream:
            stream.write((args.output / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
