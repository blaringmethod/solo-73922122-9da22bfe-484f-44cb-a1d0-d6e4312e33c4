"""Cross-check the interval engine against a deliberately naive per-second model."""

from __future__ import annotations

import random

from app.accounting import BranchInput, BuildingInput, compute_day_report


def reference(
    day_start: int,
    day_end: int,
    buildings: list[BuildingInput],
    powers: set[str],
    branches: list[BranchInput],
) -> dict[str, tuple[list[tuple[int, int]], list[tuple[int, int]]]]:
    """Per-second oracle: classify each second, then compress runs."""

    state_by_branch = {}
    for branch in branches:
        states = ["unknown"] * (day_end - day_start)
        for start, end, state in branch.segments:
            for second in range(max(start, day_start), min(end, day_end)):
                states[second - day_start] = state
        state_by_branch[branch.code] = states

    def reachable(second: int, optimistic: bool) -> set[str]:
        adjacency: dict[str, set[str]] = {}
        for branch in branches:
            current = state_by_branch[branch.code][second - day_start]
            if current == "off":
                continue
            if current == "unknown" and not optimistic:
                continue
            adjacency.setdefault(branch.node_a, set()).add(branch.node_b)
            adjacency.setdefault(branch.node_b, set()).add(branch.node_a)
        seen = set(powers)
        stack = list(seen)
        while stack:
            node = stack.pop()
            for neighbour in adjacency.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        return seen

    output: dict[str, tuple[list[tuple[int, int]], list[tuple[int, int]]]] = {}
    for building in buildings:
        categories: list[str] = []
        for offset in range(day_end - day_start):
            second = day_start + offset
            if building.code in reachable(second, optimistic=False):
                categories.append("powered")
            elif building.code in reachable(second, optimistic=True):
                categories.append("pending")
            else:
                categories.append("outage")

        runs: dict[str, list[tuple[int, int]]] = {"outage": [], "pending": []}
        index = 0
        while index < len(categories):
            category = categories[index]
            start = day_start + index
            index += 1
            while index < len(categories) and categories[index] == category:
                index += 1
            end = day_start + index
            if category in runs:
                runs[category].append((start, end))
        output[building.code] = (runs["outage"], runs["pending"])
    return output


def make_case(rng: random.Random) -> tuple[int, int, list[BuildingInput], set[str], list[BranchInput]]:
    # small day length keeps the oracle fast while still exercising boundaries
    day_start = 0
    day_end = rng.randint(20, 60)
    nodes = [f"N{i}" for i in range(rng.randint(3, 6))]
    buildings = [BuildingInput(node, node) for node in nodes[: rng.randint(1, 3)]]
    power_count = rng.randint(1, 2)
    powers = set(nodes[-power_count:])
    # avoid a node being both
    buildings = [b for b in buildings if b.code not in powers]
    if not buildings:
        buildings = [BuildingInput(nodes[0] if nodes[0] not in powers else nodes[1], "B")]

    branches = []
    for index in range(rng.randint(1, 6)):
        node_a, node_b = rng.sample(nodes, 2)
        segments = []
        cursor = day_start
        while cursor < day_end:
            if rng.random() < 0.35:
                cursor += rng.randint(1, 6)  # gap -> unknown
                continue
            end = min(day_end, cursor + rng.randint(1, 8))
            segments.append((cursor, end, rng.choice(["on", "off"])))
            cursor = end
        branches.append(BranchInput(f"L{index}", node_a, node_b, tuple(segments)))
    return day_start, day_end, buildings, powers, branches


def test_engine_matches_per_second_oracle():
    rng = random.Random(20261121)
    for _ in range(200):
        day_start, day_end, buildings, powers, branches = make_case(rng)
        expected = reference(day_start, day_end, buildings, powers, branches)
        actual = compute_day_report(day_start, day_end, buildings, powers, branches)
        by_code = {result.code: result for result in actual}
        assert set(by_code) == {b.code for b in buildings}
        for building in buildings:
            assert list(by_code[building.code].outages) == expected[building.code][0]
            assert list(by_code[building.code].pending) == expected[building.code][1]
