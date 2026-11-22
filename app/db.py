"""SQLite schema and connection helpers.

The service deliberately keeps SQL close to the business code: the project is
small and the important invariants (one current report version, immutable old
reports, whole-day branch replacement) are easier to verify when they are
visible in one place.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS topology_day (
    day        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node (
    day  TEXT NOT NULL,
    code TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('building', 'power')),
    name TEXT NOT NULL,
    PRIMARY KEY (day, code),
    FOREIGN KEY (day) REFERENCES topology_day (day)
);

CREATE TABLE IF NOT EXISTS branch (
    day       TEXT NOT NULL,
    code      TEXT NOT NULL,
    name      TEXT NOT NULL,
    node_a    TEXT NOT NULL,
    node_b    TEXT NOT NULL,
    PRIMARY KEY (day, code),
    FOREIGN KEY (day, node_a) REFERENCES node (day, code),
    FOREIGN KEY (day, node_b) REFERENCES node (day, code)
);

CREATE TABLE IF NOT EXISTS branch_segment (
    day         TEXT NOT NULL,
    branch_code TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    start_sec   INTEGER NOT NULL,
    end_sec     INTEGER NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('on', 'off')),
    PRIMARY KEY (day, branch_code, seq),
    FOREIGN KEY (day, branch_code) REFERENCES branch (day, code)
);

CREATE TABLE IF NOT EXISTS report (
    day          TEXT NOT NULL,
    version      INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (day, version)
);

CREATE TABLE IF NOT EXISTS report_building (
    day          TEXT NOT NULL,
    version      INTEGER NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT NOT NULL,
    outages_json TEXT NOT NULL,
    pending_json TEXT NOT NULL,
    PRIMARY KEY (day, version, code),
    FOREIGN KEY (day, version) REFERENCES report (day, version)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection configured for the API's concurrency model."""

    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        isolation_level=None,  # explicit BEGIN/COMMIT below
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_INIT_LOCK = threading.Lock()


def init_db(db_path: str | Path) -> None:
    """Create the schema and put a file-backed database into WAL mode.

    Safe to call from several API workers at once: the in-process lock covers
    threads and SQLite's own locking plus ``IF NOT EXISTS`` covers processes.
    """

    with _INIT_LOCK:
        conn = connect(db_path)
        try:
            if str(db_path) != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()
