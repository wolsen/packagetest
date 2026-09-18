from __future__ import annotations

import uuid
from collections import deque

from .models import BuildPlan, BuildState, PackageDefinition, PlannedBuild


def build_plan(
    *,
    definitions: dict[str, PackageDefinition],
    requested_sources: list[str],
    openstack_target: str,
    ubuntu_release: str,
    include_dependency_closure: bool = True,
    snapshot_at: str | None = None,
) -> BuildPlan:
    selected: set[str] = set()
    if include_dependency_closure:
        to_visit = deque(requested_sources)
        while to_visit:
            source = to_visit.popleft()
            if source in selected:
                continue
            if source not in definitions:
                raise KeyError(f"Unknown source package: {source}")
            selected.add(source)
            to_visit.extend(definitions[source].build_depends_on_sources)
    else:
        for source in requested_sources:
            if source not in definitions:
                raise KeyError(f"Unknown source package: {source}")
            selected.add(source)

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


def _topological_order(definitions: dict[str, PackageDefinition], selected: set[str]) -> list[str]:
    indegree = {s: 0 for s in selected}
    children: dict[str, list[str]] = {s: [] for s in selected}
    for source in selected:
        for dep in definitions[source].build_depends_on_sources:
            if dep in selected:
                indegree[source] += 1
                children[dep].append(source)

    queue = deque(sorted(s for s, i in indegree.items() if i == 0))
    out: list[str] = []
    while queue:
        node = queue.popleft()
        out.append(node)
        for child in sorted(children[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(out) != len(selected):
        raise ValueError("Dependency graph contains a cycle")
    return out


def initial_states(plan: BuildPlan) -> dict[str, BuildState]:
    return {p.source_package: BuildState.WAITING_FOR_DEPENDENCY for p in plan.planned_builds}
