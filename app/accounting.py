"""Pure accounting logic for a UTC day.

Rules implemented here
----------------------
* A branch is bidirectional and has a state at each second: ``on``, ``off``
  or ``unknown`` (a gap in its recorded segments).
* A building is *certainly* powered when a path of ``on`` branches connects it
  to any all-day power source.
* If no such certain path exists, unknown branches are optimistically treated
  as ``on``.  If even then there is no path, the building is confirmed to be
  without power (``outage``).
* If the optimistic path exists but the certain one does not, the period is
  ``pending`` verification.
* Intervals are half-open ``[start, end)`` in whole UTC seconds and are merged
  per building and category.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass

ON = "on"
OFF = "off"
UNKNOWN = "unknown"

CATEGORY_POWERED = "powered"
CATEGORY_PENDING = "pending"
CATEGORY_OUTAGE = "outage"


@dataclass(frozen=True)
class BuildingInput:
    code: str
    name: str


@dataclass(frozen=True)
class BranchInput:
    code: str
    node_a: str
    node_b: str
    # (start_sec, end_sec, state), sorted, non-overlapping
    segments: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True)
class BuildingResult:
    code: str
    name: str
    outages: tuple[tuple[int, int], ...]
    pending: tuple[tuple[int, int], ...]


def _reachable(adjacency: dict[str, set[str]], sources: Iterable[str]) -> set[str]:
    seen = set(sources)
    stack = list(seen)
    while stack:
        node = stack.pop()
        for neighbour in adjacency.get(node, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return seen


def compute_day_report(
    day_start: int,
    day_end: int,
    buildings: list[BuildingInput],
    power_codes: set[str],
    branches: list[BranchInput],
) -> list[BuildingResult]:
    """Classify every second of the day for every building.

    The day is split at every segment endpoint.  Within each resulting atomic
    interval every branch has one constant state, so reachability only needs
    to be computed once per interval.  Adjacent intervals with the same
    classification are merged afterwards.
    """

    if day_end <= day_start:
        raise ValueError("day_end must be greater than day_start")

    boundaries: set[int] = {day_start, day_end}
    for branch in branches:
        for start, end, _state in branch.segments:
            boundaries.add(start)
            boundaries.add(end)
    points = sorted(boundaries)

    # pointer into each branch's sorted segment list for the current interval
    pointers: dict[str, int] = {branch.code: 0 for branch in branches}

    # category -> list of (start, end)
    collected: dict[str, dict[str, list[tuple[int, int]]]] = {
        b.code: {CATEGORY_OUTAGE: [], CATEGORY_PENDING: []} for b in buildings
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
                category = CATEGORY_POWERED
            elif code in optimistic:
                category = CATEGORY_PENDING
            else:
                category = CATEGORY_OUTAGE

            previous = open_interval.get(code)
            if previous is not None and previous[0] == category:
                continue
            if previous is not None and previous[0] != CATEGORY_POWERED:
                collected[code][previous[0]].append((previous[1], left))
            open_interval[code] = (category, left)

    for building in buildings:
        code = building.code
        previous = open_interval[code]
        if previous[0] != CATEGORY_POWERED:
            collected[code][previous[0]].append((previous[1], day_end))

    results: list[BuildingResult] = []
    for building in buildings:
        data = collected[building.code]
        results.append(
            BuildingResult(
                code=building.code,
                name=building.name,
                outages=tuple(data[CATEGORY_OUTAGE]),
                pending=tuple(data[CATEGORY_PENDING]),
            )
        )
    return results
