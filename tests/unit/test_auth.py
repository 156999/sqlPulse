import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.auth import hash_password, require_user, verify_password
from app.config import settings
from app.routers import auth as auth_router
from app.routers import datagen, monitor, pages, reports, tasks
from app.routers.auth import validate_registration


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    old_conn = db._conn
    db._conn = None
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db.init_schema()
    yield
    db._conn = old_conn


@pytest.fixture
def auth_app(tmp_db):
    test_app = FastAPI()
    test_app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret-key-for-sqlpulse",
        session_cookie="test_session",
    )

    @test_app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    test_app.include_router(auth_router.router)
    for router in (pages.router, datagen.router, tasks.router, monitor.router, reports.router):
        test_app.include_router(router, dependencies=[Depends(require_user)])
    return test_app


@pytest.fixture
def client(auth_app):
    with TestClient(auth_app, follow_redirects=False) as c:
        yield c


def test_password_hash_roundtrip():
    raw = "secret-password"
    encoded = hash_password(raw)
    assert encoded != raw
    assert encoded.startswith("$argon2")
    assert verify_password(raw, encoded)
    assert not verify_password("wrong-password", encoded)


def test_root_seed_is_created_once(tmp_db):
    root = db.get_user_by_username("root")
    assert root is not None
    assert verify_password("root", root["password_hash"])

    db.init_schema()
    again = db.get_user_by_username("root")
    assert again["password_hash"] == root["password_hash"]


def test_create_user_and_case_insensitive_unique(tmp_db):
    user = db.create_user("Alice", "not-a-real-hash")
    assert db.get_user(user["id"])["username"] == "Alice"
    assert db.get_user_by_username("alice")["id"] == user["id"]

    with pytest.raises(sqlite3.IntegrityError):
        db.create_user("ALICE", "another-hash")


def test_registration_validation():
    assert validate_registration("ab", "password1", "password1")
    assert validate_registration("valid-name", "short", "short")
    assert validate_registration("valid-name", "password1", "password2")
    assert validate_registration("valid-name", "x" * 129, "x" * 129)
    assert validate_registration("valid-name", "password1", "password1") is None


def test_unauthenticated_pages_and_api(client):
    r = client.get("/runs/new")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    assert client.get("/api/datagen/jobs").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_register_login_logout_flow(client):
    r = client.post(
        "/register",
        data={
            "username": "tester",
            "password": "password1",
            "password_confirm": "password1",
        },
    )
    assert r.status_code == 303
    assert client.get("/runs/new").status_code == 200
    assert "tester" in client.get("/runs/new").text
    assert client.get("/register").status_code == 303
    assert client.get("/login").status_code == 303

    client.post("/logout")
    assert client.get("/runs/new").status_code == 303
    assert client.get("/api/datagen/jobs").status_code == 401


def test_duplicate_register_is_rejected(client):
    client.post(
        "/register",
        data={
            "username": "tester",
            "password": "password1",
            "password_confirm": "password1",
        },
    )
    client.post("/logout")
    r = client.post(
        "/register",
        data={
            "username": "Tester",
            "password": "password2",
            "password_confirm": "password2",
        },
    )
    assert r.status_code == 400
    assert "用户名已存在" in r.text


def test_register_frontend_rules_are_enforced(client):
    r = client.post(
        "/register",
        data={
            "username": "ab",
            "password": "password1",
            "password_confirm": "password1",
        },
    )
    assert r.status_code == 400
    assert "用户名长度" in r.text

    r = client.post(
        "/register",
        data={
            "username": "tester",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert r.status_code == 400
    assert "密码长度" in r.text


def test_root_login(client):
    r = client.post("/login", data={"username": "root", "password": "root"})
    assert r.status_code == 303
    assert client.get("/runs/new").status_code == 200


def test_session_survives_client_restart(auth_app):
    with TestClient(auth_app, follow_redirects=False) as c1:
        r = c1.post("/login", data={"username": "root", "password": "root"})
        assert r.status_code == 303
        session_cookie = c1.cookies.get("test_session")
        assert session_cookie

    with TestClient(auth_app, follow_redirects=False) as c2:
        r = c2.get("/runs/new", headers={"cookie": f"test_session={session_cookie}"})
        assert r.status_code == 200


def test_auth_disabled_allows_anonymous_access(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert client.get("/runs/new").status_code == 200
    assert client.get("/api/datagen/jobs").status_code == 200


def test_register_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_allow_register", False)
    assert client.get("/register").status_code == 303
    r = client.post(
        "/register",
        data={
            "username": "tester",
            "password": "password1",
            "password_confirm": "password1",
        },
    )
    assert r.status_code == 303
