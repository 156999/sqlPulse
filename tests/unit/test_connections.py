import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.auth import hash_password, require_user
from app.config import settings
from app.models import DbDsn
from app.routers import auth as auth_router
from app.routers import connections, datagen, pages, tasks
from app.services import connection_manager
from app.services.datagen import datagen_executor
from app.services.runner import now_iso, runner


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    old_conn = db._conn
    db._conn = None
    monkeypatch.setattr(settings, "data_dir", tmp_path)
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
    for router in (pages.router, connections.router, tasks.router, datagen.router):
        test_app.include_router(router, dependencies=[Depends(require_user)])
    return test_app


@pytest.fixture
def client(api_app):
    with TestClient(api_app, follow_redirects=False) as c:
        c.post("/login", data={"username": "root", "password": "root"})
        yield c


def _conn_payload(name="订单库", password="secret123"):
    return {
        "name": name,
        "host": "127.0.0.1",
        "port": 3306,
        "user": "app_user",
        "password": password,
        "database": "sqlpulse_demo",
    }


def _create_via_api(client, name="订单库", password="secret123"):
    r = client.post("/api/connections", json=_conn_payload(name, password))
    assert r.status_code == 201, r.text
    return r.json()


def test_run_preflight_happens_before_connection_or_persistence(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid templates must not connect, persist, or start")
    monkeypatch.setattr(connection_manager, "test_dsn", unexpected)
    monkeypatch.setattr(db, "create_run", unexpected)
    monkeypatch.setattr(runner, "start", unexpected)
    response = client.post("/api/runs", json={
        "name": "invalid", "sql_content": "SELECT {{var('missing')}};",
        "concurrency": 1, "spawn_rate": 1, "duration_sec": 5,
        "db_dsn": _conn_payload(),
    })
    assert response.status_code == 400
    assert "未定义变量" in response.json()["detail"]


def test_run_variables_persist_and_return(client, monkeypatch):
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    monkeypatch.setattr(runner, "start", lambda run_id: None)
    definitions = {"order_id": "rand(1,10000)"}
    response = client.post("/api/runs", json={
        "name": "variables", "sql_content": "SELECT {{var('order_id')}};",
        "variables": definitions,
        "concurrency": 1, "spawn_rate": 1, "duration_sec": 5,
        "db_dsn": _conn_payload(),
    })
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    assert json.loads(db.get_run(run_id)["variables_json"]) == definitions
    assert client.get(f"/api/runs/{run_id}").json()["variables"] == definitions


def test_datagen_shared_placeholders_and_row_count_are_persisted(client, monkeypatch):
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    monkeypatch.setattr(datagen_executor, "start", lambda job_id: None)
    response = client.post("/api/datagen/jobs", json={
        "name": "变量造数", "mode": "sql", "source": "paste",
        "content": "INSERT INTO t VALUES ({{rand(1,9)}}, {{var('id')}});",
        "variables": {"id": "rand(10,20)"}, "row_count": 123,
        "db_dsn": _conn_payload(),
    })
    assert response.status_code == 201, response.text
    job = db.get_datagen_job(response.json()["job_id"])
    payload = json.loads(job["input_json"])
    assert payload["row_count"] == 123 and payload["variables"]["id"] == "rand(10,20)"


def test_datagen_rejects_invalid_shared_placeholder_before_connect(client, monkeypatch):
    monkeypatch.setattr(connection_manager, "test_dsn", lambda *_: pytest.fail("must validate first"))
    response = client.post("/api/datagen/jobs", json={
        "name": "非法造数", "mode": "sql", "content": "INSERT INTO t VALUES ({{rand(2,1)}});",
        "db_dsn": _conn_payload(),
    })
    assert response.status_code == 400


def test_db_crud_respects_user_isolation(tmp_db):
    user_a = db.create_user("alice", hash_password("pw"))
    user_b = db.create_user("bob", hash_password("pw"))
    fields = {
        "id": "conn-1",
        "name": "订单库",
        "host": "db-a",
        "port": 3306,
        "user": "app",
        "password": "pw",
        "database": "orders",
        "created_at": "2026-09-18T00:00:00",
        "updated_at": "2026-09-18T00:00:00",
    }
    row = db.create_connection(user_a["id"], fields)
    assert row["user_id"] == user_a["id"]

    assert len(db.list_connections(user_a["id"])) == 1
    assert db.list_connections(user_b["id"]) == []
    assert db.get_connection(user_b["id"], "conn-1") is None
    assert db.update_connection(user_b["id"], "conn-1", {"name": "偷改"}) is None
    assert not db.delete_connection(user_b["id"], "conn-1")
    assert db.get_connection(user_a["id"], "conn-1") is not None
    assert db.delete_connection(user_a["id"], "conn-1")


def test_db_unique_name_per_user_only(tmp_db):
    user_a = db.create_user("alice", hash_password("pw"))
    user_b = db.create_user("bob", hash_password("pw"))
    base = {
        "id": "c1",
        "name": "同名库",
        "host": "db-a",
        "port": 3306,
        "user": "app",
        "password": "",
        "database": "orders",
        "created_at": "now",
        "updated_at": "now",
    }
    db.create_connection(user_a["id"], base)
    db.create_connection(user_b["id"], {**base, "id": "c2"})
    with pytest.raises(sqlite3.IntegrityError):
        db.create_connection(user_a["id"], {**base, "id": "c3"})


def test_connection_api_crud_and_password_semantics(client):
    conn = _create_via_api(client)
    assert conn["has_password"] is True
    assert "password" not in conn

    listed = client.get("/api/connections").json()
    assert len(listed) == 1
    assert "password" not in listed[0]
    assert listed[0]["id"] == conn["id"]

    # 密码留空表示保持原密码
    payload = _conn_payload()
    payload["password"] = ""
    r = client.put(f"/api/connections/{conn['id']}", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["has_password"] is True
    root_id = db.get_user_by_username("root")["id"]
    row = db.get_connection(root_id, conn["id"])
    assert row["password"] == "secret123"

    # 提交新密码则更新
    payload["password"] = "new-secret"
    r = client.put(f"/api/connections/{conn['id']}", json=payload)
    assert r.status_code == 200 and r.json()["has_password"] is True
    row = db.get_connection(root_id, conn["id"])
    assert row["password"] == "new-secret"

    r = client.delete(f"/api/connections/{conn['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/connections/{conn['id']}").status_code == 404


def test_same_name_conflict_for_same_user(client):
    _create_via_api(client, name="压测库")
    r = client.post("/api/connections", json=_conn_payload(name=" 压测库 "))
    assert r.status_code == 400
    assert "已存在" in r.json()["detail"]


def test_saved_connection_is_used_for_run_snapshot(client, monkeypatch):
    conn = _create_via_api(client)
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    monkeypatch.setattr(runner, "start", lambda run_id: None)

    body = {
        "name": "连接快照压测",
        "sql_source": "paste",
        "sql_content": "SELECT 1;",
        "concurrency": 1,
        "spawn_rate": 1,
        "duration_sec": 5,
        "connection_id": conn["id"],
    }
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    run = db.get_run(r.json()["run_id"])
    snapshot = json.loads(run["db_dsn_json"])
    assert snapshot == {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "app_user",
        "password": "secret123",
        "database": "sqlpulse_demo",
    }

    # 删除连接后快照仍然可读
    assert client.delete(f"/api/connections/{conn['id']}").status_code == 200
    assert db.get_run(run["id"])["db_dsn_json"] == run["db_dsn_json"]


def test_saved_connection_is_used_for_datagen_snapshot(client, monkeypatch):
    conn = _create_via_api(client)
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    monkeypatch.setattr(datagen_executor, "start", lambda job_id: None)

    body = {
        "name": "连接快照造数",
        "mode": "sql",
        "source": "paste",
        "content": "INSERT INTO t VALUES (1);",
        "connection_id": conn["id"],
    }
    r = client.post("/api/datagen/jobs", json=body)
    assert r.status_code == 201, r.text
    job = db.get_datagen_job(r.json()["job_id"])
    snapshot = json.loads(job["target_dsn_json"])
    assert snapshot["host"] == "127.0.0.1"
    assert snapshot["user"] == "app_user"
    assert snapshot["password"] == "secret123"


def test_connection_and_dsn_conflict_returns_400(client):
    body = {
        "name": "t",
        "sql_source": "paste",
        "sql_content": "SELECT 1;",
        "concurrency": 1,
        "spawn_rate": 1,
        "duration_sec": 5,
        "connection_id": "x",
        "db_dsn": {"host": "h"},
    }
    assert client.post("/api/runs", json=body).status_code == 400
    body.pop("connection_id")
    body.pop("db_dsn")
    assert client.post("/api/runs", json=body).status_code == 400


def test_legacy_db_dsn_request_still_works(client, monkeypatch):
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    monkeypatch.setattr(runner, "start", lambda run_id: None)
    body = {
        "name": "旧请求",
        "sql_source": "paste",
        "sql_content": "SELECT 1;",
        "concurrency": 1,
        "spawn_rate": 1,
        "duration_sec": 5,
        "db_dsn": {
            "host": "db-old",
            "port": 3307,
            "user": "root",
            "password": "legacy",
            "database": "old_db",
        },
    }
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    run = db.get_run(r.json()["run_id"])
    assert json.loads(run["db_dsn_json"]) == body["db_dsn"]


def test_connection_owned_by_other_user_is_invisible(client, api_app, tmp_db):
    user_b = db.create_user("bob", hash_password("pw"))
    db.create_connection(
        user_b["id"],
        {
            "id": "conn-b",
            "name": "Bob 的库",
            "host": "db-b",
            "port": 3306,
            "user": "root",
            "password": "secret",
            "database": "b_db",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    assert client.get("/api/connections").json() == []
    assert client.get("/api/connections/conn-b").status_code == 404
    assert client.put("/api/connections/conn-b", json=_conn_payload()).status_code == 404
    assert client.delete("/api/connections/conn-b").status_code == 404
    assert client.post("/api/connections/conn-b/test").status_code == 404


def test_test_dsn_error_never_leaks_password(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("connect failed (password: secret123)")

    monkeypatch.setattr(connection_manager.pymysql, "connect", boom)
    result = connection_manager.test_dsn(
        DbDsn(
            host="127.0.0.1",
            port=3306,
            user="app_user",
            password="secret123",
            database="sqlpulse_demo",
        )
    )
    assert result["ok"] is False
    assert "secret123" not in str(result)
    assert result["error"] == "connect failed (password: ******)"


def test_anonymous_space_when_auth_disabled(api_app, tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    with TestClient(api_app, follow_redirects=False) as anon:
        r = anon.post("/api/connections", json=_conn_payload(name="匿名库"))
        assert r.status_code == 201, r.text
        conn_id = r.json()["id"]
        assert len(anon.get("/api/connections").json()) == 1
        assert anon.post(f"/api/connections/{conn_id}/test").status_code == 200

    # 重新启用鉴权后，旧匿名连接不会出现在真实用户的列表
    with TestClient(api_app, follow_redirects=False) as c:
        c.post("/login", data={"username": "root", "password": "root"})
        assert c.get("/api/connections").json() == []


def test_connections_page_renders(client):
    _create_via_api(client, name="页面库")
    r = client.get("/connections")
    assert r.status_code == 200
    assert "连接管理" in r.text
    assert "页面库" in r.text
    assert "secret123" not in r.text


def test_temporary_and_saved_pages_include_connection_mode(client):
    _create_via_api(client, name="订单库")
    for path in ("/runs/new", "/datagen"):
        r = client.get(path)
        assert r.status_code == 200
        assert "已保存连接" in r.text
        assert "临时连接" in r.text
        assert "订单库" in r.text
