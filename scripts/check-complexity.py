#!/usr/bin/env python3
"""Reject complexity growth; legacy exceptions live in one baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from radon.complexity import cc_visit

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "config/complexity-baseline.json"
LIMIT = 5


def measured_complexity() -> dict[str, int]:
    """Measure functions, methods, and closures across production code and scripts."""
    scores = {}

    def collect(block, prefix, path):
        """Record qualified function names while keeping closures independently visible."""
        name = prefix + block.name
        if hasattr(block, "methods"):
            for method in block.methods:
                collect(method, name + ".", path)
        else:
            scores[f"{path}:{name}"] = block.complexity
            for closure in block.closures:
                collect(closure, name + ".", path)

    for directory in ("src", "scripts"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            for block in cc_visit(path.read_text()):
                if not getattr(block, "is_method", False):
                    collect(block, "", path.relative_to(ROOT).as_posix())
    return scores


def complexity_errors(scores: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Reject regressions and require removal or reduction of stale exceptions."""
    errors = [
        f"{name}: {score} exceeds {baseline.get(name, LIMIT)}"
        for name, score in scores.items()
        if score > baseline.get(name, LIMIT)
    ]
    errors.extend(
        f"{name}: remove or lower stale baseline entry"
        for name, score in baseline.items()
        if scores.get(name, 0) < score
    )
    return errors


def main() -> int:
    """Check complexity against the migration baseline or explicitly regenerate it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Record exceptions for explicit review; never run automatically in CI",
    )
    args = parser.parse_args()
    scores = measured_complexity()
    if args.write_baseline:
        BASELINE.write_text(
            json.dumps(
                {name: score for name, score in scores.items() if score > LIMIT},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    baseline = json.loads(BASELINE.read_text())
    errors = complexity_errors(scores, baseline)
    for error in errors:
        print(error)
    print(f"Checked {len(scores)} functions; {len(baseline)} legacy exceptions.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
