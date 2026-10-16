"""Select and order source packages for advisory build plans."""
from __future__ import annotations

import uuid
from collections import deque

from .models import BuildPlan, PackageDefinition, PlannedBuild


def build_plan(
    *,
    definitions: dict[str, PackageDefinition],
    requested_sources: list[str],
    openstack_target: str,
    ubuntu_release: str,
    include_dependency_closure: bool = True,
    snapshot_at: str | None = None,
) -> BuildPlan:
    """Select requested sources and produce a dependency-ordered advisory plan."""
    selected = _select_sources(definitions, requested_sources, include_dependency_closure)
    order = _topological_order(definitions, selected)
    planned = [
        PlannedBuild(
            source_package=source,
            depends_on_sources=[d for d in definitions[source].build_depends_on_sources if d in selected],
            package=definitions[source],
        )
        for source in order
    ]

    return BuildPlan(
        generation_id=f"gen-{uuid.uuid4()}",
        openstack_target=openstack_target,
        ubuntu_release=ubuntu_release,
        planned_builds=planned,
        snapshot_at=snapshot_at,
    )


def _select_sources(definitions: dict[str, PackageDefinition], requested: list[str],
                    include_dependencies: bool) -> set[str]:
    """Validate requested sources and optionally walk their transitive dependencies."""
    selected: set[str] = set()
    to_visit = deque(requested)
    while to_visit:
        source = to_visit.popleft()
        if source in selected:
            continue

        if source not in definitions:
            raise KeyError(f"Unknown source package: {source}")

        selected.add(source)
        if include_dependencies:
            to_visit.extend(definitions[source].build_depends_on_sources)

    return selected


class DependencyOrder:
    """Own remaining edge counts while ordering one selected dependency graph."""

    def __init__(self, definitions: dict[str, PackageDefinition], selected: set[str]) -> None:
        """Index only edges whose producers are part of this build selection."""
        self.indegree = {source: 0 for source in selected}
        self.children: dict[str, list[str]] = {source: [] for source in selected}
        for source in selected:
            self._register(source, definitions[source].build_depends_on_sources)

    def _register(self, source: str, dependencies: list[str]) -> None:
        """Retain duplicate edges so each has a corresponding release operation."""
        for dependency in dependencies:
            if dependency in self.indegree:
                self.indegree[source] += 1
                self.children[dependency].append(source)

    def _release_children(self, node: str, queue: deque[str]) -> None:
        """Release newly ready consumers in stable lexical order."""
        for child in sorted(self.children[node]):
            self.indegree[child] -= 1
            if self.indegree[child] == 0:
                queue.append(child)

    def ordered(self) -> list[str]:
        """Consume ready nodes and reject any remaining cyclic component."""
        queue = deque(sorted(source for source, count in self.indegree.items() if count == 0))
        out: list[str] = []
        while queue:
            node = queue.popleft()
            out.append(node)
            self._release_children(node, queue)

        if len(out) != len(self.indegree):
            raise ValueError("Dependency graph contains a cycle")

        return out


def _topological_order(definitions: dict[str, PackageDefinition], selected: set[str]) -> list[str]:
    """Return producers before consumers using the selected subgraph only."""
    return DependencyOrder(definitions, selected).ordered()
