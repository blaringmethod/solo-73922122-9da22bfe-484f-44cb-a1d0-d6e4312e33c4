"""Straightforward interval engine kept as a test oracle for the optimised one.

This is the pre-optimisation algorithm: every branch is inspected at every
atomic interval, i.e. the cost grows with ``switch_points * all_branches``.
It intentionally shares no code with :mod:`app.accounting` so that an
optimisation bug cannot hide by being mirrored in the oracle.
"""

from __future__ import annotations

import itertools

from app.accounting import BranchInput, BuildingInput, BuildingResult

ON = "on"
OFF = "off"
UNKNOWN = "unknown"


def _reachable(adjacency: dict[str, set[str]], sources: set[str]) -> set[str]:
    seen = set(sources)
    stack = list(seen)
    while stack:
        node = stack.pop()
        for neighbour in adjacency.get(node, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return seen


def compute_day_report_reference(
    day_start: int,
    day_end: int,
    buildings: list[BuildingInput],
    power_codes: set[str],
    branches: list[BranchInput],
) -> list[BuildingResult]:
    boundaries: set[int] = {day_start, day_end}
    for branch in branches:
        for start, end, _state in branch.segments:
            boundaries.add(start)
            boundaries.add(end)
    points = sorted(boundaries)

    pointers: dict[str, int] = {branch.code: 0 for branch in branches}
    collected: dict[str, dict[str, list[tuple[int, int]]]] = {
        building.code: {"outage": [], "pending": []} for building in buildings
    }
    open_interval: dict[str, tuple[str, int]] = {}

    for left, right in itertools.pairwise(points):
        if right <= left:
            continue

        adjacency_certain: dict[str, set[str]] = {}
        adjacency_optimistic: dict[str, set[str]] = {}

        for branch in branches:
            index = pointers[branch.code]
            segments = branch.segments
            while index < len(segments) and segments[index][1] <= left:
                index += 1
            pointers[branch.code] = index
            if index < len(segments) and segments[index][0] <= left < segments[index][1]:
                current = segments[index][2]
            else:
                current = UNKNOWN

            if current == ON:
                adjacency_certain.setdefault(branch.node_a, set()).add(branch.node_b)
                adjacency_certain.setdefault(branch.node_b, set()).add(branch.node_a)
            elif current == UNKNOWN:
                adjacency_optimistic.setdefault(branch.node_a, set()).add(branch.node_b)
                adjacency_optimistic.setdefault(branch.node_b, set()).add(branch.node_a)

        certain = _reachable(adjacency_certain, power_codes)
        optimistic_graph = {
            node: set(neighbours) for node, neighbours in adjacency_certain.items()
        }
        for node, neighbours in adjacency_optimistic.items():
            optimistic_graph.setdefault(node, set()).update(neighbours)
        optimistic = _reachable(optimistic_graph, power_codes)

        for building in buildings:
            code = building.code
            if code in certain:
                category = "powered"
            elif code in optimistic:
                category = "pending"
            else:
                category = "outage"

            previous = open_interval.get(code)
            if previous is not None and previous[0] == category:
                continue
            if previous is not None and previous[0] != "powered":
                collected[code][previous[0]].append((previous[1], left))
            open_interval[code] = (category, left)

    for building in buildings:
        code = building.code
        previous = open_interval[code]
        if previous[0] != "powered":
            collected[code][previous[0]].append((previous[1], day_end))

    return [
        BuildingResult(
            code=building.code,
            name=building.name,
            outages=tuple(collected[building.code]["outage"]),
            pending=tuple(collected[building.code]["pending"]),
        )
        for building in buildings
    ]
