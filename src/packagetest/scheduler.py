from __future__ import annotations

from .models import COMPLETED_SUCCESS_STATES, TERMINAL_FAILURE_STATES, BuildPlan, BuildState


def next_ready_packages(plan: BuildPlan, states: dict[str, BuildState]) -> list[str]:
    ready: list[str] = []
    build_by_source = {b.source_package: b for b in plan.planned_builds}
    for source, state in states.items():
        if state != BuildState.WAITING_FOR_DEPENDENCY:
            continue
        deps = build_by_source[source].depends_on_sources
        dep_states = [states[d] for d in deps]
        if any(s in TERMINAL_FAILURE_STATES for s in dep_states):
            states[source] = BuildState.BLOCKED_BY_FAILED_DEPENDENCY
            continue
        if all(s in COMPLETED_SUCCESS_STATES for s in dep_states):
            ready.append(source)
    return sorted(ready)


def mark_state(states: dict[str, BuildState], source: str, state: BuildState) -> None:
    states[source] = state
