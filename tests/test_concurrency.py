"""Concurrency: failed corrections must leave no trace, racing ones serialise."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.main import create_app

DAY = "2026-11-21"
BASE = f"/days/{DAY}"


def register(client: TestClient) -> None:
    payload = {
        "buildings": [{"code": "B1", "name": "1号楼"}],
        "power_sources": [{"code": "P1", "name": "配电房"}],
        "branches": [
            {
                "code": "L1",
                "name": "主干",
                "node_a": "P1",
                "node_b": "B1",
                "segments": [],
            }
        ],
    }
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 201, response.text


def _correction(state: str, expected_version: int) -> dict:
    return {
        "expected_version": expected_version,
        "segments": [
            {
                "start": "2026-11-21T00:00:00Z",
                "end": "2026-11-22T00:00:00Z",
                "state": state,
            }
        ],
    }


def test_many_racing_same_version_yield_exactly_one_success(tmp_path):
    path = str(tmp_path / "race.db")
    app = create_app(path)
    with TestClient(app) as client:
        register(client)

    barrier = threading.Barrier(8)
    statuses: list[int] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        with TestClient(app) as client:
            barrier.wait()
            response = client.post(
                f"{BASE}/branches/L1/corrections",
                json=_correction("on" if index % 2 else "off", 1),
            )
            with lock:
                statuses.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == 7

    with TestClient(app) as client:
        versions = client.get(f"{BASE}/versions").json()["versions"]
        assert [item["version"] for item in versions] == [1, 2]
        assert versions[-1]["current"] is True


def test_invalid_correction_after_conflict_window_keeps_original(tmp_path):
    path = str(tmp_path / "keep.db")
    app = create_app(path)
    with TestClient(app) as client:
        register(client)
        ok = client.post(f"{BASE}/branches/L1/corrections", json=_correction("on", 1))
        assert ok.status_code == 200

        # valid body but stale version: must not write segments or a report row
        stale = client.post(f"{BASE}/branches/L1/corrections", json=_correction("off", 1))
        assert stale.status_code == 409

        report = client.get(f"{BASE}/report").json()
        assert report["version"] == 2
        assert report["buildings"][0]["outages"] == []
        assert report["buildings"][0]["pending"] == []

        topology = client.get(f"{BASE}/topology").json()
        assert topology["branches"][0]["segments"] == [
            {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"}
        ]
        versions = client.get(f"{BASE}/versions").json()["versions"]
        assert len(versions) == 2


def test_historical_version_readable_during_recompute(tmp_path):
    path = str(tmp_path / "history.db")
    app = create_app(path)
    with TestClient(app) as client:
        register(client)
        v1 = client.get(f"{BASE}/report").json()
        assert v1["buildings"][0]["pending"] == [
            {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z"}
        ]

        assert client.post(f"{BASE}/branches/L1/corrections", json=_correction("off", 1)).status_code == 200
        v2 = client.get(f"{BASE}/report").json()
        assert v2["buildings"][0]["outages"] == [
            {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z"}
        ]

        # old report unchanged and still served
        old = client.get(f"{BASE}/report", params={"version": 1}).json()
        assert old == v1
