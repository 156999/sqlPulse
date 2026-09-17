import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BASE = "http://127.0.0.1:8080"
DSN = {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "mysql123456", "database": "sqlpulse_demo"}
ASSETS = Path(__file__).resolve().parents[1] / "assets"

pytestmark = pytest.mark.integration


def _create(client, name, sql_path, duration=10, concurrency=5):
    body = {
        "name": name, "sql_source": "paste",
        "sql_content": (ASSETS / sql_path).read_text(encoding="utf-8"),
        "concurrency": concurrency, "spawn_rate": 5, "duration_sec": duration,
        "db_dsn": DSN,
    }
    r = client.post(f"{BASE}/api/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def _wait_status(client, run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"{BASE}/api/runs/{run_id}")
        status = r.json()["status"]
        if status in ("finished", "failed", "cancelled"):
            return status
        time.sleep(1)
    raise TimeoutError(run_id)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{BASE}/login", data={"username": "root", "password": "root"})
        assert r.status_code == 303, r.text
        yield c


def test_healthz(client):
    r = client.get(f"{BASE}/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_auth_roundtrip(client):
    username = f"e2e_{int(time.time())}"
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/runs/new")
        assert r.status_code == 303, r.text
        assert r.headers["location"].startswith("/login")

        r = c.post(
            f"{BASE}/register",
            data={
                "username": username,
                "password": "password1",
                "password_confirm": "password1",
            },
        )
        assert r.status_code == 303, r.text
        assert c.get(f"{BASE}/runs/new").status_code == 200
        c.post(f"{BASE}/logout")
        assert c.get(f"{BASE}/api/datagen/jobs").status_code == 401


def test_db_test(client):
    r = client.post(f"{BASE}/api/db/test", json=DSN)
    assert r.json()["ok"] is True


def test_good_sql_e2e(client):
    run_id = _create(client, "it-good", "good.sql", duration=10)
    status = _wait_status(client, run_id)
    assert status == "finished"
    r = client.get(f"{BASE}/api/runs/{run_id}/metrics")
    points = r.json()["points"]
    assert 5 <= len(points) <= 15  # ≈10s 采样
    assert any(p["qps"] and p["qps"] > 0 for p in points)
    r = client.get(f"{BASE}/api/runs/{run_id}/report/download")
    assert r.status_code == 200 and "压测报告" in r.text


def test_bad_sql_fails(client):
    run_id = _create(client, "it-bad", "bad.sql", duration=8)
    status = _wait_status(client, run_id)
    assert status == "failed"
    r = client.get(f"{BASE}/api/runs/{run_id}")
    assert r.json()["error_msg"]


def test_stop_run(client):
    run_id = _create(client, "it-stop", "slow.sql", duration=60)
    time.sleep(3)
    r = client.post(f"{BASE}/api/runs/{run_id}/stop")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    time.sleep(2)
    r = client.get(f"{BASE}/api/runs/{run_id}")
    assert r.json()["status"] == "cancelled"


def test_stop_finished_conflict(client):
    run_id = _create(client, "it-conflict", "good.sql", duration=6)
    _wait_status(client, run_id)
    r = client.post(f"{BASE}/api/runs/{run_id}/stop")
    assert r.status_code == 409
