"""AI 诊断 job 的用户隔离：查询过滤、片段属主校验、不复用他人 job。"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.ai import diagnose_runner
from app.auth import hash_password
from app.config import settings
from app.routers import ai as ai_router
from app.routers import auth as auth_router

PASSWORD = "password123"


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    old_conn = db._conn
    db._conn = None
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "auth_enabled", True)
    db.init_schema()
    yield
    db._conn = old_conn


@pytest.fixture
def api_app(tmp_db):
    test_app = FastAPI()
    test_app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret-key-for-sqlpulse",
        session_cookie="test_session",
    )
    test_app.include_router(auth_router.router)
    test_app.include_router(ai_router.router)
    return test_app


def _user(username):
    return db.create_user(username, hash_password(PASSWORD))


def _login(api_app, username):
    client = TestClient(api_app, follow_redirects=False)
    response = client.post("/login", data={"username": username, "password": PASSWORD})
    assert response.status_code == 303, response.text
    return client


def _run(run_id="aaaaaaaa"):
    db.create_run({
        "id": run_id, "name": "isolation", "status": "finished",
        "sql_source": "paste", "sql_content": "SELECT 1;",
        "concurrency": 1, "spawn_rate": 1, "duration_sec": 1,
        "db_dsn_json": "{}", "created_at": "2026-11-08T00:00:00",
    })
    return run_id


def _job(job_id, run_id, user_id, status="pending"):
    db.create_ai_job(job_id, run_id, user_id, status)


def test_job_queries_are_scoped_to_user(tmp_db):
    alice, bob = _user("alice"), _user("bob")
    run_id = _run()
    _job("a-pending", run_id, alice["id"], "pending")
    _job("a-finished", run_id, alice["id"], "finished")
    _job("b-pending", run_id, bob["id"], "pending")

    assert db.get_latest_ai_job(run_id, alice["id"])["id"] == "a-finished"
    assert db.get_latest_ai_job(run_id, bob["id"])["id"] == "b-pending"
    assert db.get_latest_ai_job(run_id, "nobody") is None

    assert [j["id"] for j in db.list_inflight_ai_jobs(run_id, alice["id"])] == ["a-pending"]
    assert [j["id"] for j in db.list_inflight_ai_jobs(run_id, bob["id"])] == ["b-pending"]
    assert db.list_inflight_ai_jobs(run_id, "nobody") == []


def test_fragment_is_only_visible_to_job_owner(api_app, tmp_db):
    alice, bob = _user("alice"), _user("bob")
    run_id = _run()
    _job("job-a", run_id, alice["id"])

    url = f"/api/runs/{run_id}/ai/diagnose/job-a/fragment"
    assert _login(api_app, "alice").get(url).status_code == 200
    assert _login(api_app, "bob").get(url).status_code == 404


def test_start_diagnose_does_not_reuse_other_users_inflight_job(
        api_app, tmp_db, monkeypatch):
    alice, bob = _user("alice"), _user("bob")
    run_id = _run()
    _job("job-a", run_id, alice["id"])

    started = []

    def fake_start(target_run_id, user_id, *, client_factory=None):
        job_id = f"job-{user_id}"
        _job(job_id, target_run_id, user_id)
        started.append((target_run_id, user_id))
        return db.get_ai_job(job_id)

    monkeypatch.setattr(diagnose_runner, "start_job", fake_start)

    response = _login(api_app, "bob").post(f"/api/runs/{run_id}/ai/diagnose")
    assert response.status_code == 200, response.text
    assert started == [(run_id, bob["id"])]
    assert db.get_latest_ai_job(run_id, bob["id"])["id"] == f"job-{bob['id']}"
    assert db.get_latest_ai_job(run_id, alice["id"])["id"] == "job-a"
