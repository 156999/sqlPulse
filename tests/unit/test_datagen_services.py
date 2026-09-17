import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.config import settings
from app.services import datagen as datagen_module
from app.services.script_runner import build_script_env, read_result_rows, tail_file, write_script


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "logs_dir", logs_dir)
    old_conn = db._conn
    monkeypatch.setattr(db, "_conn", None)
    db.init_schema()
    yield
    db._conn = old_conn


class TestScriptRunner:
    def test_build_env_injects_dsn(self):
        dsn = {"host": "mysql", "port": 3306, "user": "root", "password": "s3cret", "database": "demo"}
        env = build_script_env(dsn)
        assert env["TARGET_DB_HOST"] == "mysql"
        assert env["TARGET_DB_PORT"] == "3306"
        assert env["TARGET_DB_USER"] == "root"
        assert env["TARGET_DB_PASSWORD"] == "s3cret"
        assert env["TARGET_DB_NAME"] == "demo"

    def test_write_script_keeps_lf(self, tmp_path):
        script = "#!/usr/bin/env bash\necho ok\r\n"
        path = write_script(tmp_path, script)
        text = path.read_bytes()
        assert text == b"#!/usr/bin/env bash\necho ok\n"

    def test_result_file_parsing(self, tmp_path):
        (tmp_path / "result.json").write_text('{"rows_affected": 42}', encoding="utf-8")
        assert read_result_rows(tmp_path) == 42
        (tmp_path / "result.json").write_text('{"rows_affected": "42"}', encoding="utf-8")
        assert read_result_rows(tmp_path) == 42
        (tmp_path / "result.json").write_text('{"rows_affected": null}', encoding="utf-8")
        assert read_result_rows(tmp_path) is None

    def test_tail_file(self, tmp_path):
        path = tmp_path / "run.log"
        path.write_text("a" * 100 + "\nerror line", encoding="utf-8")
        assert tail_file(path, size=20).endswith("error line")
        assert tail_file(tmp_path / "missing.log") == ""


class TestDataGenWorker:
    def _create_job(self, job_id="job-a"):
        db.create_datagen_job(
            {
                "id": job_id,
                "name": "demo",
                "mode": "sql",
                "status": "pending",
                "target_dsn_json": json.dumps(
                    {"host": "localhost", "port": 3306, "user": "root", "password": "p", "database": "demo"}
                ),
                "input_json": json.dumps({"source": "paste", "content": "INSERT INTO t VALUES (1);"}),
                "created_at": "2026-01-01T00:00:00",
            }
        )

    def test_worker_finished(self, temp_db, monkeypatch):
        self._create_job()
        monkeypatch.setattr(datagen_module, "execute_sql_job", lambda *a, **k: ("finished", 12, None))
        datagen_module.DataGenExecutor()._worker("job-a", threading.Event())
        job = db.get_datagen_job("job-a")
        assert job["status"] == "finished"
        assert job["rows_affected"] == 12
        assert job["ended_at"]

    def test_worker_failed(self, temp_db, monkeypatch):
        self._create_job()
        monkeypatch.setattr(datagen_module, "execute_sql_job", lambda *a, **k: ("failed", 0, "bad sql"))
        datagen_module.DataGenExecutor()._worker("job-a", threading.Event())
        job = db.get_datagen_job("job-a")
        assert job["status"] == "failed"
        assert job["error_msg"] == "bad sql"
