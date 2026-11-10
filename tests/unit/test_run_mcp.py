import json
import pytest

from app import db
from app.auth import hash_password
from app.config import settings
from app.mcp.run_server import create_run_mcp
from app.services import connection_manager
from app.services.runner import now_iso


@pytest.fixture
def run_mcp_db(monkeypatch, tmp_path):
    old_conn = db._conn
    if old_conn is not None:
        old_conn.close()
    db._conn = None
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_schema()
    user = db.create_user("loaduser", hash_password("pw"))
    db.create_connection(user["id"], {
        "id": "saved1", "name": "test", "host": "127.0.0.1", "port": 3306,
        "user": "u", "password": "secret", "database": "demo",
        "created_at": now_iso(), "updated_at": now_iso(),
    })
    yield user
    if db._conn is not None:
        db._conn.close()
    db._conn = old_conn


def test_run_mcp_registers_tools_and_hides_foreign_runs(run_mcp_db):
    alice = run_mcp_db
    bob = db.create_user("otheruser", hash_password("pw"))
    db.create_run({"id": "foreign", "owner_user_id": bob["id"], "name": "x", "status": "finished",
                   "sql_source": "paste", "sql_content": "SELECT 1", "concurrency": 1,
                   "spawn_rate": 1, "duration_sec": 5, "db_dsn_json": "{}", "created_at": now_iso()})
    server = create_run_mcp(alice)

    names = set(server._tool_manager._tools)
    assert {"preview_run", "create_run", "get_run", "get_run_metrics", "stop_run", "get_run_report"} <= names

    # Tool functions are locally registered by FastMCP; exercise the shared ownership boundary.
    from app.services.run_jobs import require_owned_run
    try:
        require_owned_run(alice["id"], "foreign")
        assert False, "foreign run must be hidden"
    except KeyError:
        pass


def test_run_insert_persists_owner(run_mcp_db):
    user = run_mcp_db
    db.create_run({"id": "owned", "owner_user_id": user["id"], "name": "x", "status": "pending",
                   "sql_source": "paste", "sql_content": "SELECT 1", "concurrency": 1,
                   "spawn_rate": 1, "duration_sec": 5, "db_dsn_json": json.dumps({}), "created_at": now_iso()})
    assert db.get_run("owned")["owner_user_id"] == user["id"]
