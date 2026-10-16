"""Debian dependency restrictions evaluated for the build target, not the host."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import subprocess


@dataclass(frozen=True)
class BuildContext:
    """Inputs that determine whether a dependency restriction is active."""

    architecture: str = "amd64"
    profiles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.architecture):
            raise ValueError(f"Invalid build architecture: {self.architecture!r}")
        if not isinstance(self.profiles, frozenset):
            raise ValueError("Build profiles must be an immutable frozenset")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", p) for p in self.profiles):
            raise ValueError("Invalid build profile")


@lru_cache(maxsize=256)
def architecture_matches(architecture: str, pattern: str) -> bool:
    """Delegate Debian architecture wildcards to the packaging toolchain."""
    result = subprocess.run(
        ["dpkg-architecture", f"-a{architecture}", f"-i{pattern}"],
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ValueError(f"Cannot evaluate architecture restriction: {pattern}")
    return result.returncode == 0


@dataclass(frozen=True)
class DependencyRelation:
    package: str
    qualifier: str | None
    operator: str | None
    version: str | None
    architectures: tuple[str, ...]
    profile_groups: tuple[tuple[str, ...], ...]
    expression: str

    def applies(self, context: BuildContext) -> bool:
        if self.architectures:
            negative = self.architectures[0].startswith("!")
            matched = any(
                architecture_matches(context.architecture, p.lstrip("!"))
                for p in self.architectures
            )
            if matched == negative:
                return False
        # Profile terms within a group are ANDed; separate groups are ORed.
        return not self.profile_groups or any(
            all(
                (term[1:] not in context.profiles)
                if term.startswith("!")
                else term in context.profiles
                for term in group
            )
            for group in self.profile_groups
        )


_RELATION = re.compile(
    r"(?P<package>[a-z0-9][a-z0-9+.-]*)"
    r"(?::(?P<qualifier>[a-z0-9][a-z0-9-]*))?"
    r"(?:\s*\((?P<operator><<|<=|=|>=|>>)\s*(?P<version>[^()\s]+)\))?"
    r"(?:\s*\[(?P<architectures>[^\[\]]+)\])?"
    r"(?P<profiles>(?:\s*<[^<>]+>)*)\s*"
)
_TERM = re.compile(r"!?[a-z0-9][a-z0-9+.-]*")


def parse_relation(expression: str) -> DependencyRelation:
    """Parse the entire relation; never silently accept an unknown suffix."""
    match = _RELATION.fullmatch(expression.strip())
    if match is None:
        raise ValueError(f"Unsupported Debian relation: {expression!r}")
    architectures = tuple((match["architectures"] or "").split())
    profiles = tuple(
        tuple(group.split()) for group in re.findall(r"<([^<>]+)>", match["profiles"])
    )
    terms = (*architectures, *(term for group in profiles for term in group))
    if any(_TERM.fullmatch(term) is None for term in terms):
        raise ValueError(f"Invalid dependency restriction: {expression!r}")
    if len({term.startswith("!") for term in architectures}) > 1:
        raise ValueError(
            "Architecture restrictions cannot mix positive and negative terms"
        )
    if any(not group for group in profiles):
        raise ValueError("Empty build profile restriction")
    return DependencyRelation(
        match["package"],
        match["qualifier"],
        match["operator"],
        match["version"],
        architectures,
        profiles,
        expression.strip(),
    )


def is_deferred_relation(expression: str) -> bool:
    """Recognize substitutions resolved later by dpkg or autopkgtest."""
    return (
        expression in {"@", "@builddeps@", "@recommends@"}
        or re.fullmatch(r"\$\{[A-Za-z0-9][A-Za-z0-9:.-]*\}", expression) is not None
    )
