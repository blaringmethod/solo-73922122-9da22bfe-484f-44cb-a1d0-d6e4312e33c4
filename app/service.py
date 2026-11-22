"""Service layer: registration, report generation, reading and corrections.

Every mutating operation runs inside a single ``BEGIN IMMEDIATE`` SQLite
transaction.  That gives us two properties the task requires:

* a correction checks the expected version and writes both the branch record
  and the new report version in one atomic step;
* two corrections racing on the same version are serialised by SQLite, so at
  most one can observe the expected version and commit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from . import accounting
from .accounting import BranchInput, BuildingInput
from .db import connect
from .errors import ApiError
from .schemas import (
    BuildingReportOut,
    CorrectionIn,
    DayRegistration,
    IntervalOut,
    NodeIn,
    ReportOut,
    ReportVersionOut,
    ReportVersionsOut,
    SegmentIn,
    SegmentOut,
    TopologyBranchOut,
    TopologyNodeOut,
    TopologyOut,
)
from .timeutil import day_bounds, format_epoch, now_iso

DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def parse_day(raw: str) -> date:
    if DAY_RE.fullmatch(raw) is None:
        raise ApiError(422, "invalid_day", "day must use YYYY-MM-DD format", day=raw)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(422, "invalid_day", "day is not a valid calendar date", day=raw) from exc


def parse_instant(raw: str) -> int:
    """Parse an ISO-8601 timestamp into whole UTC epoch seconds."""

    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(
            422,
            "invalid_timestamp",
            "timestamps must be ISO-8601, for example 2026-11-21T00:00:00Z",
            value=raw,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(422, "invalid_timestamp", "timestamps must include a timezone", value=raw)
    if parsed.microsecond:
        raise ApiError(
            422,
            "invalid_timestamp",
            "timestamps must have whole-second precision",
            value=raw,
        )
    return int(parsed.astimezone(UTC).timestamp())


def normalize_segments(
    day_start: int, day_end: int, segments: list[SegmentIn]
) -> list[tuple[int, int, str]]:
    """Validate, clip-check and sort one branch's whole-day segment list."""

    normalized: list[tuple[int, int, str]] = []
    for segment in segments:
        start = parse_instant(segment.start)
        end = parse_instant(segment.end)
        if start < day_start or end > day_end:
            raise ApiError(
                422,
                "segment_out_of_day",
                "segments must lie inside the UTC day [00:00:00Z, next 00:00:00Z)",
                start=segment.start,
                end=segment.end,
            )
        if start >= end:
            raise ApiError(
                422,
                "invalid_segment",
                "segment start must be strictly before its end",
                start=segment.start,
                end=segment.end,
            )
        normalized.append((start, end, segment.state))

    normalized.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, state in normalized:
        if merged and start < merged[-1][1]:
            raise ApiError(
                422,
                "overlapping_segments",
                "segments of the same branch must not overlap",
                start=format_epoch(start),
                end=format_epoch(end),
            )
        merged.append((start, end, state))
    return merged


def _ensure_unique_nodes(nodes: list[NodeIn], label: str) -> None:
    seen: set[str] = set()
    for node in nodes:
        if node.code in seen:
            raise ApiError(422, "duplicate_node", f"duplicate {label} code", node=node.code)
        seen.add(node.code)


def validate_registration(day_start: int, day_end: int, body: DayRegistration) -> tuple[
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str, str, str, list[tuple[int, int, str]]]],
]:
    _ensure_unique_nodes(body.buildings, "building")
    _ensure_unique_nodes(body.power_sources, "power source")

    building_codes = {node.code for node in body.buildings}
    power_codes = {node.code for node in body.power_sources}
    overlap = building_codes & power_codes
    if overlap:
        raise ApiError(
            422,
            "node_kind_conflict",
            "a node cannot be both a building and a power source",
            codes=sorted(overlap),
        )
    node_codes = building_codes | power_codes

    branch_codes: set[str] = set()
    validated_branches: list[tuple[str, str, str, str, list[tuple[int, int, str]]]] = []
    for branch in body.branches:
        if branch.code in branch_codes:
            raise ApiError(422, "duplicate_branch", "duplicate branch code", branch=branch.code)
        branch_codes.add(branch.code)
        if branch.node_a == branch.node_b:
            raise ApiError(
                422,
                "invalid_branch",
                "a branch must connect two different nodes",
                branch=branch.code,
            )
        missing = [code for code in (branch.node_a, branch.node_b) if code not in node_codes]
        if missing:
            raise ApiError(
                422,
                "unknown_node",
                "branch references a node that was not registered",
                branch=branch.code,
                nodes=missing,
            )
        segments = normalize_segments(day_start, day_end, branch.segments)
        validated_branches.append((branch.code, branch.name, branch.node_a, branch.node_b, segments))

    buildings = [(node.code, node.name) for node in body.buildings]
    powers = [(node.code, node.name) for node in body.power_sources]
    return buildings, powers, validated_branches


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------


@contextmanager
def _conn(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _day_exists(conn: sqlite3.Connection, day: str) -> bool:
    row = conn.execute("SELECT 1 FROM topology_day WHERE day = ?", (day,)).fetchone()
    return row is not None


def _current_version(conn: sqlite3.Connection, day: str) -> int | None:
    row = conn.execute("SELECT MAX(version) AS version FROM report WHERE day = ?", (day,)).fetchone()
    assert row is not None
    return row["version"]


def _load_topology(conn: sqlite3.Connection, day: str) -> tuple[
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str, str, str, list[tuple[int, int, str]]]],
]:
    building_rows = conn.execute(
        "SELECT code, name FROM node WHERE day = ? AND kind = 'building' ORDER BY code",
        (day,),
    ).fetchall()
    power_rows = conn.execute(
        "SELECT code, name FROM node WHERE day = ? AND kind = 'power' ORDER BY code",
        (day,),
    ).fetchall()
    branch_rows = conn.execute(
        "SELECT code, name, node_a, node_b FROM branch WHERE day = ? ORDER BY code",
        (day,),
    ).fetchall()
    segment_rows = conn.execute(
        "SELECT branch_code, start_sec, end_sec, state FROM branch_segment "
        "WHERE day = ? ORDER BY branch_code, seq",
        (day,),
    ).fetchall()

    segments_by_branch: dict[str, list[tuple[int, int, str]]] = {}
    for row in segment_rows:
        segments_by_branch.setdefault(row["branch_code"], []).append(
            (row["start_sec"], row["end_sec"], row["state"])
        )

    buildings = [(row["code"], row["name"]) for row in building_rows]
    powers = [(row["code"], row["name"]) for row in power_rows]
    branches = [
        (
            row["code"],
            row["name"],
            row["node_a"],
            row["node_b"],
            segments_by_branch.get(row["code"], []),
        )
        for row in branch_rows
    ]
    return buildings, powers, branches


def _compute(
    day_start: int,
    day_end: int,
    buildings: list[tuple[str, str]],
    powers: list[tuple[str, str]],
    branches: list[tuple[str, str, str, str, list[tuple[int, int, str]]]],
) -> list[accounting.BuildingResult]:
    return accounting.compute_day_report(
        day_start=day_start,
        day_end=day_end,
        buildings=[BuildingInput(code=code, name=name) for code, name in buildings],
        power_codes={code for code, _name in powers},
        branches=[
            BranchInput(
                code=code,
                node_a=node_a,
                node_b=node_b,
                segments=tuple(sorted(segments)),
            )
            for code, _name, node_a, node_b, segments in branches
        ],
    )


def _store_report(
    conn: sqlite3.Connection,
    day: str,
    version: int,
    generated_at: str,
    results: list[accounting.BuildingResult],
) -> None:
    conn.execute(
        "INSERT INTO report (day, version, generated_at) VALUES (?, ?, ?)",
        (day, version, generated_at),
    )
    for result in results:
        conn.execute(
            "INSERT INTO report_building "
            "(day, version, code, name, outages_json, pending_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                day,
                version,
                result.code,
                result.name,
                json.dumps([list(interval) for interval in result.outages]),
                json.dumps([list(interval) for interval in result.pending]),
            ),
        )


def _intervals_out(raw: list[list[int]]) -> list[IntervalOut]:
    return [IntervalOut(start=format_epoch(start), end=format_epoch(end)) for start, end in raw]


def _report_out(conn: sqlite3.Connection, day: str, version: int) -> ReportOut:
    report = conn.execute(
        "SELECT generated_at FROM report WHERE day = ? AND version = ?",
        (day, version),
    ).fetchone()
    if report is None:
        raise ApiError(404, "report_not_found", "report version not found", day=day, version=version)
    rows = conn.execute(
        "SELECT code, name, outages_json, pending_json FROM report_building "
        "WHERE day = ? AND version = ? ORDER BY code",
        (day, version),
    ).fetchall()

    buildings: list[BuildingReportOut] = []
    for row in rows:
        outages = _intervals_out(json.loads(row["outages_json"]))
        pending = _intervals_out(json.loads(row["pending_json"]))
        if pending:
            status = "needs_verification"
        elif outages:
            status = "confirmed_outage"
        else:
            status = "powered"
        buildings.append(
            BuildingReportOut(
                code=row["code"],
                name=row["name"],
                status=status,
                outages=outages,
                pending=pending,
            )
        )
    return ReportOut(
        day=day,
        version=version,
        generated_at=report["generated_at"],
        buildings=buildings,
    )


# ---------------------------------------------------------------------------
# public operations
# ---------------------------------------------------------------------------


def register_day(db_path: str, raw_day: str, body: DayRegistration) -> ReportOut:
    day = parse_day(raw_day).isoformat()
    day_start, day_end = day_bounds(date.fromisoformat(day))
    buildings, powers, branches = validate_registration(day_start, day_end, body)

    with _conn(db_path) as conn:
        with _immediate(conn):
            if _day_exists(conn, day):
                raise ApiError(
                    409,
                    "day_already_registered",
                    "a topology is already registered for this UTC day",
                    day=day,
                )
            created_at = now_iso()
            conn.execute(
                "INSERT INTO topology_day (day, created_at) VALUES (?, ?)",
                (day, created_at),
            )
            for node in body.buildings:
                conn.execute(
                    "INSERT INTO node (day, code, kind, name) VALUES (?, ?, 'building', ?)",
                    (day, node.code, node.name),
                )
            for node in body.power_sources:
                conn.execute(
                    "INSERT INTO node (day, code, kind, name) VALUES (?, ?, 'power', ?)",
                    (day, node.code, node.name),
                )
            for code, name, node_a, node_b, segments in branches:
                conn.execute(
                    "INSERT INTO branch (day, code, name, node_a, node_b) VALUES (?, ?, ?, ?, ?)",
                    (day, code, name, node_a, node_b),
                )
                for seq, (start, end, state) in enumerate(segments):
                    conn.execute(
                        "INSERT INTO branch_segment (day, branch_code, seq, start_sec, end_sec, state) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (day, code, seq, start, end, state),
                    )
            results = _compute(day_start, day_end, buildings, powers, branches)
            _store_report(conn, day, 1, created_at, results)

        return _report_out(conn, day, 1)


def get_report(db_path: str, raw_day: str, version: int | None = None) -> ReportOut:
    day = parse_day(raw_day).isoformat()
    with _conn(db_path) as conn:
        if not _day_exists(conn, day):
            raise ApiError(404, "day_not_found", "no topology is registered for this day", day=day)
        if version is None:
            current = _current_version(conn, day)
            if current is None:
                raise ApiError(404, "report_not_found", "no report has been generated", day=day)
            version = current
        return _report_out(conn, day, version)


def list_report_versions(db_path: str, raw_day: str) -> ReportVersionsOut:
    day = parse_day(raw_day).isoformat()
    with _conn(db_path) as conn:
        if not _day_exists(conn, day):
            raise ApiError(404, "day_not_found", "no topology is registered for this day", day=day)
        current = _current_version(conn, day)
        rows = conn.execute(
            "SELECT version, generated_at FROM report WHERE day = ? ORDER BY version",
            (day,),
        ).fetchall()
        return ReportVersionsOut(
            day=day,
            versions=[
                ReportVersionOut(
                    version=row["version"],
                    generated_at=row["generated_at"],
                    current=row["version"] == current,
                )
                for row in rows
            ],
        )


def get_topology(db_path: str, raw_day: str) -> TopologyOut:
    day = parse_day(raw_day).isoformat()
    with _conn(db_path) as conn:
        if not _day_exists(conn, day):
            raise ApiError(404, "day_not_found", "no topology is registered for this day", day=day)
        current = _current_version(conn, day)
        buildings, powers, branches = _load_topology(conn, day)
        return TopologyOut(
            day=day,
            current_version=current or 0,
            buildings=[TopologyNodeOut(code=code, name=name) for code, name in buildings],
            power_sources=[TopologyNodeOut(code=code, name=name) for code, name in powers],
            branches=[
                TopologyBranchOut(
                    code=code,
                    name=name,
                    node_a=node_a,
                    node_b=node_b,
                    segments=[
                        SegmentOut(start=format_epoch(start), end=format_epoch(end), state=state)
                        for start, end, state in segments
                    ],
                )
                for code, name, node_a, node_b, segments in branches
            ],
        )


def correct_branch(
    db_path: str, raw_day: str, branch_code: str, body: CorrectionIn
) -> tuple[int, ReportOut]:
    """Replace one branch's whole-day record and publish a new report version."""

    day = parse_day(raw_day).isoformat()
    day_start, day_end = day_bounds(date.fromisoformat(day))
    segments = normalize_segments(day_start, day_end, body.segments)

    with _conn(db_path) as conn:
        with _immediate(conn):
            if not _day_exists(conn, day):
                raise ApiError(404, "day_not_found", "no topology is registered for this day", day=day)
            current = _current_version(conn, day)
            if current is None:
                raise ApiError(404, "report_not_found", "no report has been generated", day=day)
            if body.expected_version != current:
                raise ApiError(
                    409,
                    "version_conflict",
                    "expected_version is not the current report version",
                    day=day,
                    expected_version=body.expected_version,
                    current_version=current,
                )
            branch = conn.execute(
                "SELECT code FROM branch WHERE day = ? AND code = ?",
                (day, branch_code),
            ).fetchone()
            if branch is None:
                raise ApiError(
                    404,
                    "branch_not_found",
                    "no such branch for this day",
                    day=day,
                    branch=branch_code,
                )

            conn.execute(
                "DELETE FROM branch_segment WHERE day = ? AND branch_code = ?",
                (day, branch_code),
            )
            for seq, (start, end, state) in enumerate(segments):
                conn.execute(
                    "INSERT INTO branch_segment (day, branch_code, seq, start_sec, end_sec, state) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (day, branch_code, seq, start, end, state),
                )

            buildings, powers, branches = _load_topology(conn, day)
            results = _compute(day_start, day_end, buildings, powers, branches)
            new_version = current + 1
            _store_report(conn, day, new_version, now_iso(), results)

        return new_version, _report_out(conn, day, new_version)
