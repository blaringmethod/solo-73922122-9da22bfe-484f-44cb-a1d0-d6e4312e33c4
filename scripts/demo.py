#!/usr/bin/env python3
"""End-to-end demo against a running instance.

Usage: python scripts/demo.py [base_url]
Default base URL: http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
DAY = "2026-11-21"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def show(title: str, value: object) -> None:
    print(f"\n### {title}")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    status, health = call("GET", "/health")
    show(f"GET /health -> {status}", health)

    registration = {
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
                    {"start": f"{DAY}T00:00:00Z", "end": f"{DAY}T08:00:00Z", "state": "on"},
                    {"start": f"{DAY}T10:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"},
                ],
            },
            {
                "code": "L2",
                "name": "A2 主干",
                "node_a": "P1",
                "node_b": "A2",
                "segments": [
                    {"start": f"{DAY}T00:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "off"}
                ],
            },
        ],
    }
    status, report = call("POST", f"/days/{DAY}/topology", registration)
    show(f"POST /days/{DAY}/topology -> {status} (v1: A1 has an 08:00-10:00 unknown gap, A2 is off all day)", report)

    correction = {
        "expected_version": 1,
        "segments": [
            {"start": f"{DAY}T00:00:00Z", "end": f"{DAY}T06:00:00Z", "state": "off"},
            {"start": f"{DAY}T06:00:00Z", "end": "2026-11-22T00:00:00Z", "state": "on"},
        ],
    }
    status, corrected = call("POST", f"/days/{DAY}/branches/L2/corrections", correction)
    show(f"POST /days/{DAY}/branches/L2/corrections (expected_version=1) -> {status} (v2)", corrected)

    status, old = call("GET", f"/days/{DAY}/report?version=1")
    show(f"GET /days/{DAY}/report?version=1 -> {status} (old report still readable)", old)

    status, stale = call("POST", f"/days/{DAY}/branches/L2/corrections", correction)
    show(f"POST correction again with expected_version=1 -> {status} (stale version rejected)", stale)

    status, versions = call("GET", f"/days/{DAY}/versions")
    show(f"GET /days/{DAY}/versions -> {status}", versions)


if __name__ == "__main__":
    main()
