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

Performance model
-----------------
A day usually has many branches that never change state and only a few that
switch often.  Rebuilding the whole graph at every switch point would make the
cost proportional to ``switching_points * all_branches``.

Instead the day is handled in three steps:

1. Each branch is compressed into contiguous state intervals.  A branch with a
   single state interval is *stable*; otherwise it is *switching*.
2. The stable ``on`` edges are contracted into connected components once (and,
   for the optimistic graph, stable ``unknown`` edges are contracted too).
   These components are the reusable "connectivity summary" of the topology.
3. Only the switching branches contribute atomic interval boundaries.  For
   every interval a small reachability search runs over the contracted
   components using just the switching branches' current edges.

The stable part is therefore scanned once, not once per switch point, and no
per-second topology is ever materialised.  Both the certain (known ``on`` only)
and optimistic (known ``on`` plus recorded gaps) graphs are contracted and
searched separately.
"""

from __future__ import annotations

import itertools
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


class _DisjointSet:
    """Union-find used once to contract the stable part of the topology."""

    __slots__ = ("_parent", "_rank")

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, item: int) -> int:
        parent = self._parent
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        rank = self._rank
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1


def _state_intervals(
    day_start: int, day_end: int, segments: tuple[tuple[int, int, str], ...]
) -> tuple[tuple[int, int, str], ...]:
    """Turn one branch's recorded segments into contiguous state intervals.

    Gaps between segments become ``unknown`` intervals.  Adjacent intervals
    with the same state are merged, so a branch whose state never changes
    yields exactly one interval and is treated as stable.
    """

    pieces: list[tuple[int, int, str]] = []
    cursor = day_start
    for raw_start, raw_end, state in segments:
        start = max(raw_start, day_start)
        end = min(raw_end, day_end)
        if start >= end:
            continue
        if start > cursor:
            pieces.append((cursor, start, UNKNOWN))
            cursor = start
        if end > cursor:
            pieces.append((cursor, end, state))
            cursor = end
    if cursor < day_end:
        pieces.append((cursor, day_end, UNKNOWN))

    merged: list[tuple[int, int, str]] = []
    for start, end, state in pieces:
        if merged and merged[-1][1] == start and merged[-1][2] == state:
            merged[-1] = (merged[-1][0], end, state)
        else:
            merged.append((start, end, state))
    return tuple(merged)


def _reachable_components(
    power_components: set[int], edges: list[tuple[int, int]]
) -> set[int]:
    """Reachability over contracted components using only dynamic edges."""

    if not edges:
        return power_components

    adjacency: dict[int, list[int]] = {}
    for left, right in edges:
        if left == right:
            continue
        adjacency.setdefault(left, []).append(right)
        adjacency.setdefault(right, []).append(left)

    seen = set(power_components)
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
    *,
    diagnostics: dict[str, int] | None = None,
) -> list[BuildingResult]:
    """Classify every second of the day for every building.

    Stable branches are contracted into connected components once.  Only
    switching branches add interval boundaries, and each interval only walks
    the switching branches, never the full branch list.

    ``diagnostics`` is an optional empty dict filled with counters used by the
    benchmark and regression tests; it does not affect the result.
    """

    if day_end <= day_start:
        raise ValueError("day_end must be greater than day_start")

    # ------------------------------------------------------------------
    # 1. compress every branch into contiguous state intervals
    # ------------------------------------------------------------------
    states = [_state_intervals(day_start, day_end, branch.segments) for branch in branches]

    # ------------------------------------------------------------------
    # 2. build the node universe and contract the stable edges
    # ------------------------------------------------------------------
    node_codes = set(power_codes)
    node_codes.update(building.code for building in buildings)
    for branch in branches:
        node_codes.add(branch.node_a)
        node_codes.add(branch.node_b)
    node_index = {code: index for index, code in enumerate(sorted(node_codes))}

    certain_dsu = _DisjointSet(len(node_index))
    optimistic_dsu = _DisjointSet(len(node_index))

    boundaries: set[int] = {day_start, day_end}
    dynamic: list[tuple[tuple[tuple[int, int, str], ...], int, int]] = []
    stable_branch_count = 0

    for branch, intervals in zip(branches, states):
        left_node = node_index[branch.node_a]
        right_node = node_index[branch.node_b]
        if len(intervals) == 1:
            stable_branch_count += 1
            stable_state = intervals[0][2]
            if stable_state == ON:
                certain_dsu.union(left_node, right_node)
                optimistic_dsu.union(left_node, right_node)
            elif stable_state == UNKNOWN:
                optimistic_dsu.union(left_node, right_node)
            # OFF contributes no edge to either graph.
        else:
            boundaries.update(interval[0] for interval in intervals[1:])
            dynamic.append((intervals, left_node, right_node))
    # Map switching-branch endpoints through the finished stable components.
    dynamic_components = [
        (
            intervals,
            certain_dsu.find(left_node),
            certain_dsu.find(right_node),
            optimistic_dsu.find(left_node),
            optimistic_dsu.find(right_node),
        )
        for intervals, left_node, right_node in dynamic
    ]

    power_certain = {certain_dsu.find(node_index[code]) for code in power_codes}
    power_optimistic = {optimistic_dsu.find(node_index[code]) for code in power_codes}

    # Buildings that share both component ids always have the same category
    # timeline, so classify each group once and reuse its interval tuples.
    building_keys: list[tuple[int, int]] = []
    groups: dict[tuple[int, int], None] = {}
    for building in buildings:
        node = node_index[building.code]
        key = (certain_dsu.find(node), optimistic_dsu.find(node))
        building_keys.append(key)
        groups.setdefault(key, None)

    collected: dict[tuple[int, int], dict[str, list[tuple[int, int]]]] = {
        key: {CATEGORY_OUTAGE: [], CATEGORY_PENDING: []} for key in groups
    }
    open_interval: dict[tuple[int, int], tuple[str, int]] = {}
    pointers = [0] * len(dynamic_components)
    interval_count = 0
    branch_checks = 0

    # ------------------------------------------------------------------
    # 3. one small search per atomic interval, over switching branches only
    # ------------------------------------------------------------------
    for left, right in itertools.pairwise(sorted(boundaries)):
        if right <= left:
            continue
        interval_count += 1

        certain_edges: list[tuple[int, int]] = []
        optimistic_edges: list[tuple[int, int]] = []

        for position, (intervals, certain_a, certain_b, optimistic_a, optimistic_b) in enumerate(
            dynamic_components
        ):
            branch_checks += 1
            pointer = pointers[position]
            while pointer + 1 < len(intervals) and intervals[pointer][1] <= left:
                pointer += 1
            pointers[position] = pointer
            state = intervals[pointer][2]

            if state == ON:
                certain_edges.append((certain_a, certain_b))
                optimistic_edges.append((optimistic_a, optimistic_b))
            elif state == UNKNOWN:
                optimistic_edges.append((optimistic_a, optimistic_b))
            # OFF: absent from both graphs.

        certain_reachable = _reachable_components(power_certain, certain_edges)
        optimistic_reachable = _reachable_components(power_optimistic, optimistic_edges)

        for key in groups:
            certain_root, optimistic_root = key
            if certain_root in certain_reachable:
                category = CATEGORY_POWERED
            elif optimistic_root in optimistic_reachable:
                category = CATEGORY_PENDING
            else:
                category = CATEGORY_OUTAGE

            previous = open_interval.get(key)
            if previous is not None and previous[0] == category:
                continue
            if previous is not None and previous[0] != CATEGORY_POWERED:
                collected[key][previous[0]].append((previous[1], left))
            open_interval[key] = (category, left)

    for key in groups:
        previous = open_interval[key]
        if previous[0] != CATEGORY_POWERED:
            collected[key][previous[0]].append((previous[1], day_end))

    results: list[BuildingResult] = []
    for building, key in zip(buildings, building_keys):
        data = collected[key]
        results.append(
            BuildingResult(
                code=building.code,
                name=building.name,
                outages=tuple(data[CATEGORY_OUTAGE]),
                pending=tuple(data[CATEGORY_PENDING]),
            )
        )

    if diagnostics is not None:
        diagnostics["total_branches"] = len(branches)
        diagnostics["stable_branches"] = stable_branch_count
        diagnostics["switching_branches"] = len(dynamic_components)
        diagnostics["atomic_intervals"] = interval_count
        diagnostics["branch_state_checks"] = branch_checks
        diagnostics["building_groups"] = len(groups)
    return results
