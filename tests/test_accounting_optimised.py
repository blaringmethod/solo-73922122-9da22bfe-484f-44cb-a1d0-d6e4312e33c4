"""Optimised engine: correctness vs the old algorithm and benchmark scenarios.

The task requires that, with many stable branches and only a few switching
ones, the optimised accounting is faster while producing identical results,
without ever rescanning all branches per switch point or expanding per-second
topology.
"""

from __future__ import annotations

import random
import time

import pytest

from app.accounting import BranchInput, BuildingInput, compute_day_report
from reference_accounting import compute_day_report_reference

DAY_START = 0
DAY_END = 24 * 60 * 60


def build_mixed_case(
    rng: random.Random,
    *,
    stable_count: int,
    switching_count: int,
    building_count: int,
    switch_every: int = 900,
    stable_on_ratio: float = 0.8,
) -> tuple[int, int, list[BuildingInput], set[str], list[BranchInput]]:
    """A day with many stable branches, a few switching ones and shared topology.

    Some stable branches are all-day ``unknown`` (no recorded segments) and the
    switching branches occasionally contain unrecorded gaps so that the sample
    exercises the optimistic graph as well as the certain one.
    """

    buildings = [BuildingInput(f"B{i}", f"B{i}") for i in range(building_count)]
    powers = {"P0", "P1"}
    extra_nodes = [f"N{i}" for i in range(stable_count + switching_count)]
    nodes = [b.code for b in buildings] + sorted(powers) + extra_nodes
    branches: list[BranchInput] = []

    for index in range(stable_count):
        node_a, node_b = rng.sample(nodes, 2)
        roll = rng.random()
        if roll < stable_on_ratio:
            segments: tuple[tuple[int, int, str], ...] = ((DAY_START, DAY_END, "on"),)
        elif roll < stable_on_ratio + 0.15:
            segments = ((DAY_START, DAY_END, "off"),)
        else:
            segments = ()  # all-day unknown
        branches.append(BranchInput(f"S{index}", node_a, node_b, segments))

    for index in range(switching_count):
        node_a, node_b = rng.sample(nodes, 2)
        segments = []
        cursor = DAY_START
        while cursor < DAY_END:
            if rng.random() < 0.1:
                cursor += min(switch_every, DAY_END - cursor)  # unrecorded gap
                continue
            end = min(DAY_END, cursor + switch_every)
            segments.append((cursor, end, rng.choice(["on", "off"])))
            cursor = end
        branches.append(BranchInput(f"W{index}", node_a, node_b, tuple(segments)))

    return DAY_START, DAY_END, buildings, powers, branches


def test_mixed_topology_matches_reference_engine():
    rng = random.Random(20261122)
    for _ in range(10):
        case = build_mixed_case(
            rng,
            stable_count=rng.randint(20, 60),
            switching_count=rng.randint(1, 4),
            building_count=rng.randint(3, 10),
            switch_every=rng.choice([300, 600, 1800]),
        )
        fast = compute_day_report(*case)
        reference = compute_day_report_reference(*case)
        assert fast == reference


def test_result_identical_when_only_stable_branches_share_component():
    """A switching branch in the same component as stable ones must not drop them."""

    # P0 --S1(on)-- HUB --S2(on)-- B1 ; HUB --W1(switching)-- B2
    buildings = [BuildingInput("B1", "B1"), BuildingInput("B2", "B2")]
    branches = [
        BranchInput("S1", "P0", "HUB", ((DAY_START, DAY_END, "on"),)),
        BranchInput("S2", "HUB", "B1", ((DAY_START, DAY_END, "on"),)),
        BranchInput(
            "W1",
            "HUB",
            "B2",
            ((DAY_START, 6 * 3600, "on"), (6 * 3600, DAY_END, "off")),
        ),
    ]
    by_code = {result.code: result for result in compute_day_report(
        DAY_START, DAY_END, buildings, {"P0"}, branches
    )}
    assert by_code["B1"].outages == ()
    assert by_code["B1"].pending == ()
    assert by_code["B2"].outages == ((6 * 3600, DAY_END),)
    assert by_code["B2"].pending == ()


def test_switching_branches_alone_add_boundaries():
    rng = random.Random(1)
    _, _, _, _, branches = build_mixed_case(
        rng, stable_count=40, switching_count=2, building_count=5, switch_every=3600
    )
    diagnostics: dict[str, int] = {}
    compute_day_report(
        DAY_START, DAY_END, [BuildingInput("B0", "B0")], {"P0"}, branches,
        diagnostics=diagnostics,
    )
    assert diagnostics["stable_branches"] == 40
    assert diagnostics["switching_branches"] == 2
    # 2 switching branches with 24 one-hour states -> at most 46 boundaries.
    assert diagnostics["atomic_intervals"] <= 48
    # Each interval inspects only switching branches, never all 42.
    assert diagnostics["branch_state_checks"] == (
        diagnostics["atomic_intervals"] * diagnostics["switching_branches"]
    )
    assert diagnostics["branch_state_checks"] < (
        diagnostics["atomic_intervals"] * diagnostics["total_branches"]
    )


@pytest.mark.parametrize(
    ("stable_count", "switching_count"),
    [(400, 2), (1200, 3)],
)
def test_optimised_engine_is_faster_on_many_stable_branches(
    stable_count: int, switching_count: int
):
    rng = random.Random(stable_count)
    case = build_mixed_case(
        rng,
        stable_count=stable_count,
        switching_count=switching_count,
        building_count=20,
        switch_every=600,
    )

    # warm-up
    compute_day_report(*case)
    compute_day_report_reference(*case)

    start = time.perf_counter()
    fast = compute_day_report(*case)
    fast_seconds = time.perf_counter() - start

    start = time.perf_counter()
    reference = compute_day_report_reference(*case)
    reference_seconds = time.perf_counter() - start

    assert fast == reference
    assert fast_seconds < reference_seconds


def test_stable_connectivity_is_reused_across_switch_points():
    """Stable edges are contracted once, so switch points never rescan them.

    The diagnostic ``branch_state_checks`` counts inspections of switching
    branches inside the interval loop.  With 300 stable branches and 3
    switching ones the per-interval work must depend only on the 3; the stable
    edges are touched a single time during contraction.
    """

    rng = random.Random(42)
    case = build_mixed_case(
        rng, stable_count=300, switching_count=3, building_count=10, switch_every=600
    )
    diagnostics: dict[str, int] = {}
    compute_day_report(*case, diagnostics=diagnostics)

    assert diagnostics["stable_branches"] == 300
    assert diagnostics["branch_state_checks"] == (
        diagnostics["atomic_intervals"] * 3
    )
    assert diagnostics["branch_state_checks"] < (
        diagnostics["atomic_intervals"] * diagnostics["total_branches"] // 10
    )
