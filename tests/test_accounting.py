"""Unit tests for the pure accounting engine."""

from __future__ import annotations

from app.accounting import BranchInput, BuildingInput, compute_day_report

DAY_START = 0
DAY_END = 24 * 60 * 60


def ts(hour: int, minute: int = 0) -> int:
    return hour * 3600 + minute * 60


def intervals(result):
    return list(result.outages), list(result.pending)


def test_unknown_branch_is_pending_not_outage():
    # building -- unknown -- power
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1")],
        {"P1"},
        [BranchInput("L1", "B1", "P1", ())],
    )[0]
    assert result.outages == ()
    assert result.pending == ((DAY_START, DAY_END),)


def test_off_branch_is_confirmed_outage():
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1")],
        {"P1"},
        [BranchInput("L1", "B1", "P1", ((DAY_START, DAY_END, "off"),))],
    )[0]
    assert result.outages == ((DAY_START, DAY_END),)
    assert result.pending == ()


def test_adjacent_intervals_merge_per_category():
    # 00:00-02:00 on to power; 02:00-03:00 unknown; 03:00-24:00 on.
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1")],
        {"P1"},
        [
            BranchInput(
                "L1",
                "B1",
                "P1",
                ((DAY_START, ts(2), "on"), (ts(3), DAY_END, "on")),
            )
        ],
    )[0]
    assert result.outages == ()
    assert result.pending == ((ts(2), ts(3)),)


def test_gap_is_unknown_even_when_bounded_by_off():
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1")],
        {"P1"},
        [
            BranchInput(
                "L1",
                "B1",
                "P1",
                ((DAY_START, ts(1), "off"), (ts(2), DAY_END, "off")),
            )
        ],
    )[0]
    assert result.outages == ((DAY_START, ts(1)), (ts(2), DAY_END))
    assert result.pending == ((ts(1), ts(2)),)


def test_redundant_paths_and_cycles():
    # B1 --L1-- P1 ; B1 --L2-- HUB ; HUB --L3-- P2
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1")],
        {"P1", "P2"},
        [
            BranchInput("L1", "B1", "P1", ()),  # unknown
            BranchInput("L2", "B1", "HUB", ((DAY_START, DAY_END, "off"),)),
            BranchInput("L3", "HUB", "P2", ((DAY_START, DAY_END, "on"),)),
        ],
    )[0]
    # certain path is blocked by L2 off; optimistic path via L1 unknown -> pending
    assert result.outages == ()
    assert result.pending == ((DAY_START, DAY_END),)


def test_bidirectional_traversal():
    # power is node_b, building is node_a; must still be reachable.
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1")],
        {"P1"},
        [BranchInput("L1", "P1", "B1", ((DAY_START, DAY_END, "on"),))],
    )[0]
    assert result.outages == ()
    assert result.pending == ()


def test_per_branch_state_changes_split_buildings_independently():
    # B1 has two parallel branches: L1 on all day, L2 flips. B2 only via L2.
    result = compute_day_report(
        DAY_START,
        DAY_END,
        [BuildingInput("B1", "B1"), BuildingInput("B2", "B2")],
        {"P1"},
        [
            BranchInput("L1", "B1", "P1", ((DAY_START, DAY_END, "on"),)),
            BranchInput(
                "L2",
                "B2",
                "P1",
                ((DAY_START, ts(12), "on"), (ts(12), DAY_END, "off")),
            ),
        ],
    )
    by_code = {item.code: item for item in result}
    assert by_code["B1"].outages == () and by_code["B1"].pending == ()
    assert by_code["B2"].outages == ((ts(12), DAY_END),)
    assert by_code["B2"].pending == ()
