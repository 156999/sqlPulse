"""多表并发调度的单元测试：分组并发、控制语句先行、失败隔离、状态汇总。

通过 monkeypatch 替换 execute_sql_job，不依赖真实数据库。
"""

import threading
import time
from pathlib import Path

from app.services import datagen as datagen_module
from app.services.datagen import DataGenExecutor

DSN = {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "", "database": "demo"}


def _payload(content, **overrides):
    payload = {"content": content, "variables": {}, "concurrency": 4, "row_count": 1}
    payload.update(overrides)
    return payload


def _install(monkeypatch, handler):
    monkeypatch.setattr(datagen_module, "execute_sql_job", handler)


def test_run_multi_groups_statements_by_target_table(tmp_path, monkeypatch):
    seen = []

    def handler(job_id, dsn, statements, log_path, event, timeout, **kwargs):
        seen.append((kwargs.get("target_table"), len(statements)))
        return "finished", len(statements), None

    _install(monkeypatch, handler)
    executor = DataGenExecutor()

    status, rows, error = executor._run_multi(
        "abc", DSN,
        _payload("INSERT INTO orders (a) VALUES (1);\n"
                 "INSERT INTO stock (b) VALUES (2);\n"
                 "INSERT INTO orders (a) VALUES (3);"),
        tmp_path / "datagen_abc.log", threading.Event())

    assert (status, error) == ("finished", None)
    assert rows == 3
    assert dict(seen) == {"orders": 2, "stock": 1}


def test_run_multi_runs_control_statements_first(tmp_path, monkeypatch):
    order = []

    def handler(job_id, dsn, statements, log_path, event, timeout, **kwargs):
        order.append(kwargs.get("target_table"))
        return "finished", 1, None

    _install(monkeypatch, handler)
    executor = DataGenExecutor()

    status, _, _ = executor._run_multi(
        "abc", DSN, _payload("SET @x = 1;\nINSERT INTO orders (a) VALUES (1);"),
        tmp_path / "d.log", threading.Event())

    assert status == "finished"
    assert order[0] is None
    assert "orders" in order


def test_run_multi_keeps_other_tables_when_one_fails(tmp_path, monkeypatch):
    def handler(job_id, dsn, statements, log_path, event, timeout, **kwargs):
        if kwargs.get("target_table") == "stock":
            return "failed", 0, "Duplicate entry '1' for key 'PRIMARY'"
        return "finished", 2, None

    _install(monkeypatch, handler)
    executor = DataGenExecutor()

    status, rows, error = executor._run_multi(
        "abc", DSN,
        _payload("INSERT INTO orders (a) VALUES (1);\nINSERT INTO stock (b) VALUES (2);"),
        tmp_path / "d.log", threading.Event())

    assert status == "partial"
    assert rows == 2
    assert "stock" in error and "Duplicate entry" in error


def test_run_multi_reports_failed_when_every_group_fails(tmp_path, monkeypatch):
    def handler(job_id, dsn, statements, log_path, event, timeout, **kwargs):
        return "failed", 0, "boom"

    _install(monkeypatch, handler)
    executor = DataGenExecutor()

    status, rows, error = executor._run_multi(
        "abc", DSN,
        _payload("INSERT INTO orders (a) VALUES (1);\nINSERT INTO stock (b) VALUES (2);"),
        tmp_path / "d.log", threading.Event())

    assert status == "failed"
    assert rows == 0
    assert "orders" in error and "stock" in error


def test_run_multi_requires_recognisable_target_table(tmp_path, monkeypatch):
    _install(monkeypatch, lambda *a, **k: ("finished", 0, None))
    executor = DataGenExecutor()

    status, rows, error = executor._run_multi(
        "abc", DSN, _payload("SET @x = 1;\nCOMMIT;"), tmp_path / "d.log", threading.Event())

    assert status == "failed"
    assert "目标表" in error


def test_run_multi_writes_per_table_logs(tmp_path, monkeypatch):
    log_files = []

    def handler(job_id, dsn, statements, log_path, event, timeout, **kwargs):
        log_files.append(Path(log_path).name)
        return "finished", 1, None

    _install(monkeypatch, handler)
    executor = DataGenExecutor()
    log_path = tmp_path / "datagen_abc.log"

    executor._run_multi("abc", DSN,
                        _payload("INSERT INTO orders (a) VALUES (1);\nINSERT INTO stock (b) VALUES (2);"),
                        log_path, threading.Event())

    assert sorted(log_files) == ["datagen_abc__orders.log", "datagen_abc__stock.log"]
    assert log_path.exists()


def test_run_multi_executes_groups_in_parallel(tmp_path, monkeypatch):
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def handler(job_id, dsn, statements, log_path, event, timeout, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return "finished", 1, None

    _install(monkeypatch, handler)
    executor = DataGenExecutor()

    executor._run_multi(
        "abc", DSN,
        _payload("INSERT INTO a (x) VALUES (1);\nINSERT INTO b (x) VALUES (2);\nINSERT INTO c (x) VALUES (3);",
                 concurrency=3),
        tmp_path / "d.log", threading.Event())

    assert state["peak"] >= 2


def test_run_multi_stops_dispatching_after_cancel(tmp_path, monkeypatch):
    event = threading.Event()
    calls = []

    def handler(job_id, dsn, statements, log_path, event_arg, timeout, **kwargs):
        calls.append(kwargs.get("target_table"))
        event.set()
        return "cancelled", 0, None

    _install(monkeypatch, handler)
    executor = DataGenExecutor()

    status, rows, _ = executor._run_multi(
        "abc", DSN, _payload("SET @x = 1;\nINSERT INTO orders (a) VALUES (1);"), tmp_path / "d.log", event)

    assert status == "cancelled"
    assert calls == [None]
