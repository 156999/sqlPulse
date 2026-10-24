import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.auth import hash_password
from app.config import settings
from app.mcp import datagen_server
from app.mcp.server import create_mcp_server, resolve_mcp_user
from app.services import connection_manager
from app.services.datagen import datagen_executor
from app.services.runner import now_iso


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    old_conn = db._conn
    monkeypatch.setattr(db, "_conn", None)
    db.init_schema()
    yield
    db._conn = old_conn


def _user(username: str) -> dict:
    return db.create_user(username, hash_password("pw"))


def _connection(user_id: str, connection_id: str) -> None:
    db.create_connection(
        user_id,
        {
            "id": connection_id,
            "name": f"{connection_id} 库",
            "host": "127.0.0.1",
            "port": 3306,
            "user": "app",
            "password": "secret",
            "database": "demo",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )


def test_mcp_lists_only_current_user_connections(temp_db):
    alice = _user("alice")
    bob = _user("bob")
    _connection(alice["id"], "conn-a")
    _connection(bob["id"], "conn-b")

    result = datagen_server.list_my_connections(alice)

    assert [c["id"] for c in result["connections"]] == ["conn-a"]
    assert "password" not in result["connections"][0]
    assert result["connections"][0]["has_password"] is True


def test_mcp_created_job_is_owned_and_hidden_from_other_users(temp_db, monkeypatch):
    alice = _user("alice")
    bob = _user("bob")
    _connection(alice["id"], "conn-a")
    monkeypatch.setattr(connection_manager, "test_dsn", lambda dsn: {"ok": True})
    monkeypatch.setattr(datagen_executor, "start", lambda job_id: None)

    created = datagen_server.create_datagen_sql_job(
        alice,
        {
            "connection_id": "conn-a",
            "name": "orders 造数",
            "sql": "INSERT INTO orders VALUES ({{var('id')}});",
            "variables": {"id": "rand(1,100)"},
            "row_count": 5,
        },
    )

    job = db.get_datagen_job(created["job_id"])
    assert job["owner_user_id"] == alice["id"]
    assert json.loads(job["input_json"])["row_count"] == 5
    assert datagen_server.get_datagen_job(alice, created["job_id"])["job_id"] == created["job_id"]
    with pytest.raises(KeyError):
        datagen_server.get_datagen_job(bob, created["job_id"])


@pytest.mark.anyio
async def test_fastmcp_server_registers_datagen_tools(temp_db):
    alice = _user("alice")
    server = create_mcp_server(alice)

    tools = await server.list_tools()

    names = {tool.name for tool in tools}
    assert {
        "list_my_connections",
        "list_datagen_tables",
        "get_datagen_table_metadata",
        "apply_datagen_rules",
        "validate_datagen_rules",
        "build_datagen_dependency_plan",
        "create_datagen_sql_job",
        "get_datagen_job",
        "get_datagen_job_log",
        "stop_datagen_job",
    } <= names


def test_resolve_mcp_user_by_username(temp_db):
    alice = _user("alice")

    assert resolve_mcp_user(username="alice")["id"] == alice["id"]
