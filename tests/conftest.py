from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client
