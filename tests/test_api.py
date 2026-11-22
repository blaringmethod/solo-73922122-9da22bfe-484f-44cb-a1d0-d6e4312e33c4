"""End-to-end API tests using FastAPI's TestClient."""

from __future__ import annotations

import threading

import pytest

DAY = "2026-11-21"
BASE = f"/days/{DAY}"


def topology_payload():
    return {
        "buildings": [
            {"code": "A1", "name": "1号楼"},
            {"code": "A2", "name": "2号楼"},
        ],
        "power_sources": [{"code": "P1", "name": "主配电房"}],
        "branches": [
            {
                "code": "L1",
                "name": "A1 主干",
                "node_a": "P1",
                "node_b": "A1",
                "segments": [
                    {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T08:00:00Z", "state": "on"},
                    {"start": "2026-11-21T10:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"},
                ],
            },
            {
                "code": "L2",
                "name": "A2 主干",
                "node_a": "P1",
                "node_b": "A2",
                "segments": [
                    {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "off"},
                ],
            },
        ],
    }


def register(client):
    response = client.post(f"{BASE}/topology", json=topology_payload())
    assert response.status_code == 201, response.text
    return response.json()


def test_register_and_read_report(client):
    report = register(client)
    assert report["day"] == DAY
    assert report["version"] == 1

    by_code = {b["code"]: b for b in report["buildings"]}
    # L1 has an 08:00-10:00 unknown gap -> A1 pending, no confirmed outage.
    assert by_code["A1"]["outages"] == []
    assert by_code["A1"]["pending"] == [
        {"start": "2026-11-21T08:00:00Z", "end": "2026-11-21T10:00:00Z"}
    ]
    assert by_code["A1"]["status"] == "needs_verification"
    # L2 is off the whole day -> A2 confirmed outage, no pending.
    assert by_code["A2"]["outages"] == [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z"}
    ]
    assert by_code["A2"]["pending"] == []
    assert by_code["A2"]["status"] == "confirmed_outage"

    fetched = client.get(f"{BASE}/report")
    assert fetched.status_code == 200
    assert fetched.json() == report


def test_duplicate_day_registration_conflicts(client):
    register(client)
    response = client.post(f"{BASE}/topology", json=topology_payload())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "day_already_registered"


def test_overlapping_segments_rejected(client):
    payload = topology_payload()
    payload["branches"][0]["segments"] = [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T05:00:00Z", "state": "on"},
        {"start": "2026-11-21T04:59:59Z", "end": "2026-11-21T09:00:00Z", "state": "off"},
    ]
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "overlapping_segments"


def test_segments_outside_day_rejected(client):
    payload = topology_payload()
    payload["branches"][0]["segments"][0]["end"] = "2026-11-22T00:00:01Z"
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "segment_out_of_day"


def test_subsecond_timestamp_rejected(client):
    payload = topology_payload()
    payload["branches"][0]["segments"][0]["start"] = "2026-11-21T00:00:00.500Z"
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_timestamp"


def test_correction_replaces_whole_day_and_bumps_version(client):
    register(client)

    correction = client.post(
        f"{BASE}/branches/L2/corrections",
        json={
            "expected_version": 1,
            "segments": [
                {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T06:00:00Z", "state": "off"},
                {"start": "2026-11-21T06:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"},
            ],
        },
    )
    assert correction.status_code == 200, correction.text
    assert correction.headers["x-report-version"] == "2"
    report = correction.json()
    assert report["version"] == 2
    by_code = {b["code"]: b for b in report["buildings"]}
    assert by_code["A2"]["outages"] == [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T06:00:00Z"}
    ]

    # topology and report both reflect the correction
    topology = client.get(f"{BASE}/topology").json()
    l2 = next(b for b in topology["branches"] if b["code"] == "L2")
    assert l2["segments"] == [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T06:00:00Z", "state": "off"},
        {"start": "2026-11-21T06:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"},
    ]

    # old report still readable while current is v2
    old = client.get(f"{BASE}/report", params={"version": 1}).json()
    assert old["version"] == 1
    assert {b["code"]: b for b in old["buildings"]}["A2"]["outages"] == [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z"}
    ]

    versions = client.get(f"{BASE}/versions").json()
    assert versions["versions"] == [
        {"version": 1, "generated_at": versions["versions"][0]["generated_at"], "current": False},
        {"version": 2, "generated_at": versions["versions"][1]["generated_at"], "current": True},
    ]


def test_stale_correction_fails_without_changing_anything(client):
    register(client)
    first = client.post(
        f"{BASE}/branches/L2/corrections",
        json={
            "expected_version": 1,
            "segments": [
                {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T06:00:00Z", "state": "off"},
                {"start": "2026-11-21T06:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"},
            ],
        },
    )
    assert first.status_code == 200

    stale = client.post(
        f"{BASE}/branches/L2/corrections",
        json={
            "expected_version": 1,
            "segments": [
                {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"}
            ],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "version_conflict",
        "message": "expected_version is not the current report version",
        "day": DAY,
        "expected_version": 1,
        "current_version": 2,
    }

    # current report and branch record unchanged
    current = client.get(f"{BASE}/report").json()
    assert current["version"] == 2
    topology = client.get(f"{BASE}/topology").json()
    l2 = next(b for b in topology["branches"] if b["code"] == "L2")
    assert l2["segments"][0]["state"] == "off"


def test_invalid_correction_does_not_change_anything(client):
    register(client)
    response = client.post(
        f"{BASE}/branches/L2/corrections",
        json={
            "expected_version": 1,
            "segments": [
                {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "off"},
                {"start": "2026-11-21T12:00:00Z", "end": "2026-11-21T13:00:00Z", "state": "on"},
            ],
        },
    )
    assert response.status_code == 422
    current = client.get(f"{BASE}/report").json()
    assert current["version"] == 1
    topology = client.get(f"{BASE}/topology").json()
    l2 = next(b for b in topology["branches"] if b["code"] == "L2")
    assert l2["segments"] == [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "off"}
    ]


def test_unknown_branch_or_day(client):
    register(client)
    assert client.post(
        f"{BASE}/branches/NOPE/corrections",
        json={"expected_version": 1, "segments": []},
    ).status_code == 404
    assert client.get("/days/2026-11-20/report").status_code == 404
    assert client.get(f"{BASE}/report", params={"version": 99}).status_code == 404


def test_concurrent_corrections_only_one_wins(tmp_path):
    """Two corrections with the same expected version: exactly one succeeds."""

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(str(tmp_path / "concurrent.db"))
    with TestClient(app) as client:
        register(client)

    results: list[int] = []
    barrier = threading.Barrier(2)

    def attempt(state: str):
        with TestClient(app) as client:
            barrier.wait()
            response = client.post(
                f"{BASE}/branches/L2/corrections",
                json={
                    "expected_version": 1,
                    "segments": [
                        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": state}
                    ],
                },
            )
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(s,)) for s in ("off", "on")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 409]
    with TestClient(app) as client:
        assert client.get(f"{BASE}/versions").json()["versions"][-1]["version"] == 2


@pytest.mark.parametrize(
    "day",
    ["2026-13-01", "not-a-day", "2026-11-21T00:00:00Z"],
)
def test_bad_day_path(client, day):
    assert client.get(f"/days/{day}/report").status_code == 422


def test_validation_error_envelope(client):
    response = client.post(f"{BASE}/topology", json={"buildings": []})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert isinstance(error["details"], list) and error["details"]


def test_adjacent_same_state_segments_are_preserved(client):
    payload = topology_payload()
    payload["branches"][1]["segments"] = [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T12:00:00Z", "state": "off"},
        {"start": "2026-11-21T12:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "off"},
    ]
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 201, response.text
    topology = client.get(f"{BASE}/topology").json()
    l2 = next(b for b in topology["branches"] if b["code"] == "L2")
    assert len(l2["segments"]) == 2


def test_empty_correction_clears_branch_to_unknown(client):
    register(client)
    response = client.post(
        f"{BASE}/branches/L2/corrections",
        json={"expected_version": 1, "segments": []},
    )
    assert response.status_code == 200, response.text
    a2 = next(b for b in response.json()["buildings"] if b["code"] == "A2")
    assert a2["outages"] == []
    assert a2["pending"] == [{"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z"}]


def test_day_with_trailing_newline_rejected(client):
    response = client.get("/days/2026-11-21%0A/report")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_day"


def test_branch_referencing_unknown_node_rejected(client):
    payload = topology_payload()
    payload["branches"][0]["node_b"] = "GHOST"
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unknown_node"
    assert error["branch"] == "L1"
    assert error["nodes"] == ["GHOST"]


def test_same_node_registered_as_building_and_power_rejected(client):
    payload = topology_payload()
    payload["power_sources"].append({"code": "A1", "name": "冒充电源"})
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "node_kind_conflict"
    assert error["codes"] == ["A1"]


def test_correction_segment_outside_day_rejected(client):
    register(client)
    response = client.post(
        f"{BASE}/branches/L2/corrections",
        json={
            "expected_version": 1,
            "segments": [
                {"start": "2026-11-20T23:59:59Z", "end": "2026-11-21T12:00:00Z", "state": "off"}
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "segment_out_of_day"
    assert client.get(f"{BASE}/report").json()["version"] == 1


def test_timestamps_with_offset_are_normalized_to_utc(client):
    payload = topology_payload()
    payload["branches"][0]["segments"] = [
        {"start": "2026-11-21T08:00:00+08:00", "end": "2026-11-21T16:00:00+08:00", "state": "on"}
    ]
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 201, response.text
    topology = client.get(f"{BASE}/topology").json()
    l1 = next(b for b in topology["branches"] if b["code"] == "L1")
    assert l1["segments"] == [
        {"start": "2026-11-21T00:00:00Z", "end": "2026-11-21T08:00:00Z", "state": "on"}
    ]
    a1 = next(b for b in response.json()["buildings"] if b["code"] == "A1")
    assert a1["pending"] == [
        {"start": "2026-11-21T08:00:00Z", "end": "2026-11-22T00:00:00Z"}
    ]


def test_building_without_any_branch_is_out_all_day(client):
    payload = topology_payload()
    payload["buildings"].append({"code": "A3", "name": "3号楼"})
    response = client.post(f"{BASE}/topology", json=payload)
    assert response.status_code == 201, response.text
    a3 = next(b for b in response.json()["buildings"] if b["code"] == "A3")
    assert a3["status"] == "confirmed_outage"
    assert a3["outages"] == [{"start": "2026-11-21T00:00:00Z", "end": "2026-11-22T00:00:00Z"}]
    assert a3["pending"] == []
