from __future__ import annotations

import re
import subprocess


_OPERATORS = {
    -1: "lt",
    0: "eq",
    1: "gt",
}

_OPENSTACK_TARGET_RE = re.compile(r"^(?P<base>\d+\.\d+)(?:-(?P<stage>b\d+|rc\d+|final))?$")
_UPSTREAM_BETA_RE = re.compile(r"^(?P<base>.+?)\.0(?P<stage>b\d+)$")
_UPSTREAM_RC_RE = re.compile(r"^(?P<base>.+?)\.0(?P<stage>rc\d+)$")


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


def openstack_target_to_upstream_version(target: str) -> str:
    match = _OPENSTACK_TARGET_RE.fullmatch(target)
    if not match:
        return target
    base = match.group("base")
    stage = match.group("stage")
    if stage in {None, "final"}:
        return base
    return f"{base}~{stage}"


def openstack_target_to_debian_version(target: str, *, ubuntu_revision: str = "0ubuntu1") -> str:
    upstream_version = openstack_target_to_upstream_version(target)
    return upstream_version_to_debian_version(upstream_version, ubuntu_revision=ubuntu_revision)


def upstream_version_to_debian_version(upstream_version: str, *, ubuntu_revision: str = "0ubuntu1") -> str:
    beta_match = _UPSTREAM_BETA_RE.fullmatch(upstream_version)
    if beta_match:
        upstream_version = f"{beta_match.group('base')}~{beta_match.group('stage')}"
    rc_match = _UPSTREAM_RC_RE.fullmatch(upstream_version)
    if rc_match:
        upstream_version = f"{rc_match.group('base')}~{rc_match.group('stage')}"
    return f"{upstream_version}-{ubuntu_revision}"
