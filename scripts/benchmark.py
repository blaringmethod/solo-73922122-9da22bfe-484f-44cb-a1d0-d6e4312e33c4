#!/usr/bin/env python3
"""Compare the optimised accounting engine with the old all-branches-per-interval one.

Both engines are run on the same synthetic day.  The sample mimics a real
property day: many branches keep one state all day and only a few switch, and
the stable and switching branches share the same connected topology.

Usage: python scripts/benchmark.py
No server is needed; the script only imports the pure accounting module.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.accounting import BranchInput, BuildingInput, compute_day_report
from reference_accounting import compute_day_report_reference

DAY_START = 0
DAY_END = 24 * 60 * 60


def build_case(
    rng: random.Random,
    stable_count: int,
    switching_count: int,
    switch_every: int,
    building_count: int = 30,
) -> tuple:
    buildings = [BuildingInput(f"B{i}", f"B{i}") for i in range(building_count)]
    powers = {"P0", "P1"}
    nodes = [b.code for b in buildings] + sorted(powers) + [
        f"N{i}" for i in range(stable_count + switching_count)
    ]
    branches: list[BranchInput] = []

    for index in range(stable_count):
        node_a, node_b = rng.sample(nodes, 2)
        state = "on" if rng.random() < 0.8 else "off"
        branches.append(BranchInput(f"S{index}", node_a, node_b, ((DAY_START, DAY_END, state),)))

    for index in range(switching_count):
        node_a, node_b = rng.sample(nodes, 2)
        segments = []
        cursor = DAY_START
        while cursor < DAY_END:
            end = min(DAY_END, cursor + switch_every)
            segments.append((cursor, end, rng.choice(["on", "off"])))
            cursor = end
        branches.append(BranchInput(f"W{index}", node_a, node_b, tuple(segments)))

    return DAY_START, DAY_END, buildings, powers, branches


def main() -> None:
    print(
        f"{'stable':>7} {'switching':>9} {'intervals/switch':>16} "
        f"{'old (ms)':>10} {'new (ms)':>10} {'speedup':>9} {'identical':>10}"
    )
    for stable_count, switching_count in [(200, 2), (1000, 3), (3000, 3), (5000, 2)]:
        case = build_case(
            random.Random(stable_count + switching_count),
            stable_count,
            switching_count,
            switch_every=600,
        )
        compute_day_report(*case)
        compute_day_report_reference(*case)

        start = time.perf_counter()
        optimised = compute_day_report(*case)
        optimised_seconds = time.perf_counter() - start

        start = time.perf_counter()
        reference = compute_day_report_reference(*case)
        reference_seconds = time.perf_counter() - start

        print(
            f"{stable_count:>7} {switching_count:>9} {144:>16} "
            f"{reference_seconds * 1000:>10.1f} {optimised_seconds * 1000:>10.1f} "
            f"{reference_seconds / optimised_seconds:>8.1f}x "
            f"{str(optimised == reference):>10}"
        )


if __name__ == "__main__":
    main()
