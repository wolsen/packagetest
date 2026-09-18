from __future__ import annotations

import subprocess


_OPERATORS = {
    -1: "lt",
    0: "eq",
    1: "gt",
}


def debian_compare(left: str, right: str) -> int:
    if _dpkg_compare(left, right, "lt"):
        return -1
    if _dpkg_compare(left, right, "gt"):
        return 1
    return 0


def _dpkg_compare(left: str, right: str, op: str) -> bool:
    cmd = ["dpkg", "--compare-versions", left, op, right]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return completed.returncode == 0


def assert_debian_order(left: str, right: str, expected: int) -> None:
    actual = debian_compare(left, right)
    if actual != expected:
        op = _OPERATORS[expected]
        raise ValueError(f"Expected {left} {op} {right}, got {actual}")
