"""Debian dependency restrictions evaluated for the build target, not the host."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import chain
import re
import subprocess


@dataclass(frozen=True)
class BuildContext:
    """Inputs that determine whether a dependency restriction is active."""

    architecture: str = "amd64"
    profiles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Reject malformed target architecture and profile names."""
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
    """A parsed dependency with target-specific activation restrictions."""

    package: str
    qualifier: str | None
    operator: str | None
    version: str | None
    architectures: tuple[str, ...]
    profile_groups: tuple[tuple[str, ...], ...]
    expression: str

    def applies(self, context: BuildContext) -> bool:
        """Require both architecture and profile restrictions to match."""
        return self._architecture_applies(context) and self._profiles_apply(context)

    def _architecture_applies(self, context: BuildContext) -> bool:
        """Match a uniformly positive or negative architecture restriction."""
        if not self.architectures:
            return True

        negative = self.architectures[0].startswith("!")
        matched = any(
            architecture_matches(context.architecture, pattern.lstrip("!"))
            for pattern in self.architectures
        )
        return matched != negative

    def _profiles_apply(self, context: BuildContext) -> bool:
        """OR profile groups, whose individual terms must all match."""
        return not self.profile_groups or any(
            _profile_group_applies(group, context.profiles)
            for group in self.profile_groups
        )


def _profile_group_applies(group: tuple[str, ...], profiles: frozenset[str]) -> bool:
    """AND the positive and negated terms within one profile group."""
    return all(
        (term[1:] not in profiles) if term.startswith("!") else term in profiles
        for term in group
    )


_RELATION = re.compile(
    r"(?P<package>[a-z0-9][a-z0-9+.-]*)"
    r"(?::(?P<qualifier>[a-z0-9][a-z0-9-]*))?"
    r"(?:\s*\((?P<operator><<|<=|=|>=|>>)\s*(?P<version>[^()\s]+)\))?"
    r"(?:\s*\[(?P<architectures>[^\[\]]+)\])?"
    r"(?P<profiles>(?:\s*<[^<>]+>)*)\s*"
)
_TERM = re.compile(r"!?[a-z0-9][a-z0-9+.-]*")


def _validate_restrictions(
    architectures: tuple[str, ...], profiles: tuple[tuple[str, ...], ...], expression: str
) -> None:
    """Reject malformed terms and contradictory restriction syntax."""
    terms = (*architectures, *chain.from_iterable(profiles))
    if any(_TERM.fullmatch(term) is None for term in terms):
        raise ValueError(f"Invalid dependency restriction: {expression!r}")

    _validate_restriction_groups(architectures, profiles)


def _validate_restriction_groups(
    architectures: tuple[str, ...], profiles: tuple[tuple[str, ...], ...]
) -> None:
    """Keep architecture polarity uniform and profile groups nonempty."""
    if len({term.startswith("!") for term in architectures}) > 1:
        raise ValueError("Architecture restrictions cannot mix positive and negative terms")

    if any(not group for group in profiles):
        raise ValueError("Empty build profile restriction")


def parse_relation(expression: str) -> DependencyRelation:
    """Parse the entire relation; never silently accept an unknown suffix."""
    match = _RELATION.fullmatch(expression.strip())
    if match is None:
        raise ValueError(f"Unsupported Debian relation: {expression!r}")

    architectures = tuple((match["architectures"] or "").split())
    profiles = tuple(
        tuple(group.split()) for group in re.findall(r"<([^<>]+)>", match["profiles"])
    )
    _validate_restrictions(architectures, profiles, expression)

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
