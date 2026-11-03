import csv
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.config import settings
from app.routers import monitor
from app.services.collector import Collector
from app.services.observability import build_event_timeline, build_snapshot
from app.services.report import build_l0
from app.services.runner import LocustRunner
from app.services.sql_history import HistoryReader, read_per_sql_history, sync_sql_history


HEADER = ["Timestamp", "Name", "Requests/s", "Total Request Count", "Total Failure Count",
          "Total Average Response Time", "95%", "99%"]


def write_history(path, rows, mode="w"):
    with path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if mode == "w":
            writer.writerow(HEADER)
        writer.writerows(rows)


@pytest.fixture
def timing_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "auth_seed_root", False)
    original = db._conn
    db._conn = None
    db.init_schema()
    (tmp_path / "locust").mkdir()
    run = dict(id="timing", name="timing", status="running", sql_source="paste",
               sql_content="SELECT 1", concurrency=1, spawn_rate=1, duration_sec=10,
               db_dsn_json="{}", created_at="2026-11-02T12:00:00")
    db.create_run(run)
    yield tmp_path / "locust" / "timing_stats_history.csv"
    db._conn.close()
    db._conn = original


def test_incremental_reader_waits_for_complete_rows_and_handles_truncate(tmp_path):
    path = tmp_path / "history.csv"
    write_history(path, [[100, "Aggregated", 10, 100, 0, 5, 10, 20],
                         [101, "Aggregated", 11, 110, 0, 5, 10, 20]])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("102,Aggregated,12,120")
    reader = HistoryReader()
    assert [r["Timestamp"] for r in reader.read(path)] == ["100", "101"]
    assert reader.read(path) == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write(",0,5,10,20\n")
    assert [r["Timestamp"] for r in reader.read(path)] == ["102"]
    write_history(path, [[200, "Aggregated", 20, 200, 0, 5, 10, 20]])
    assert [r["Timestamp"] for r in reader.read(path)] == ["200"]


def test_ingests_every_source_row_once_and_preserves_resource_time(timing_db):
    write_history(timing_db, [[100, "Aggregated", 10, 100, 0, 5, 10, 20],
                             [101, "Aggregated", 11, 110, 0, 5, 10, 700],
                             [102, "Aggregated", 12, 120, 0, 5, 10, 20]])
    db.insert_metric(dict(run_id="timing", ts=105.25, cpu_pct=81, metric_scope="resource",
                          resource_collected_at=105.25, threads_connected=2))
    first = sync_sql_history("timing")
    assert [p["ts"] for p in first] == [100, 101, 102]
    assert all(p["source_ts"] == p["ts"] and p["collected_at"] > 102 for p in first)
    assert sync_sql_history("timing") == []
    points = db.get_metrics("timing")
    assert [p["ts"] for p in points if p.get("qps") is not None] == [100, 101, 102]
    assert points[-1]["ts"] == 105.25 and points[-1]["cpu_pct"] == 81
    assert points[-1]["qps"] is None
    events = build_event_timeline(points)
    assert next(e["ts"] for e in events if e["metric"] == "p99_ms") == 101
    assert next(e["ts"] for e in events if e["metric"] == "cpu_pct") == 105.25


def test_historical_rebuild_removes_stale_sql_but_keeps_capacity(timing_db):
    db.insert_metric(dict(run_id="timing", ts=105, qps=999, request_count=90,
                          threads_connected=3, max_connections=100, cpu_pct=30))
    db.update_run("timing", {"status": "finished"})
    write_history(timing_db, [[100, "Aggregated", 10, 100, 0, 5, 10, 20],
                             [106, "Aggregated", 12, 120, 0, 5, 10, 20]])
    points = db.get_metrics("timing")
    assert [p["ts"] for p in points] == [100, 105, 106]
    assert points[1]["qps"] is None and points[1]["cpu_pct"] == 30
    assert points[0]["collected_at"] is None
    capacity = build_snapshot(db.get_run("timing"), points)["latest_capacity"]
    assert capacity["threads_connected"] == 3 and capacity["max_connections"] == 100
    filtered = db.get_metrics("timing", 105, 105)
    assert len(filtered) == 1 and filtered[0]["qps"] is None


def test_duplicate_second_keeps_latest_snapshot_and_is_idempotent(timing_db):
    write_history(timing_db, [[100, "Aggregated", 10, 100, 0, 5, 10, 20],
                             [100, "Aggregated", 11, 110, 1, 6, 12, 30],
                             [100, "sql_1", 11, 110, 1, 6, 12, 30],
                             [100, "sql_1", 12, 120, 1, 7, 14, 40]])
    points = sync_sql_history("timing")
    assert len(points) == 1 and points[0]["request_count"] == 110
    assert sync_sql_history("timing") == []
    per_sql = read_per_sql_history(timing_db)
    assert len(per_sql["sql_1"]) == 1 and per_sql["sql_1"][0]["p99_ms"] == 40


def test_same_timestamp_sql_and_resource_do_not_overwrite_each_other(timing_db):
    write_history(timing_db, [[100, "Aggregated", 10, 100, 0, 5, 10, 20]])
    sync_sql_history("timing")
    db.insert_metric(dict(run_id="timing", ts=100, cpu_pct=30, metric_scope="resource"))
    db.insert_metric(dict(run_id="timing", ts=100, threads_connected=3))
    point = db.get_metrics("timing")[0]
    assert point["cpu_pct"] == 30 and point["threads_connected"] == 3
    assert point["qps"] == 10 and point["metric_scope"] == "mixed"


def test_db_failure_does_not_lose_csv_checkpoint(timing_db, monkeypatch):
    write_history(timing_db, [[100, "Aggregated", 10, 100, 0, 5, 10, 20]])
    original = db.insert_sql_metrics
    monkeypatch.setattr(db, "insert_sql_metrics", lambda *args: (_ for _ in ()).throw(RuntimeError("busy")))
    with pytest.raises(RuntimeError):
        sync_sql_history("timing")
    monkeypatch.setattr(db, "insert_sql_metrics", original)
    assert len(sync_sql_history("timing")) == 1


def test_mysql_rates_use_actual_interval_and_survive_viewer_disconnect():
    collector = Collector()
    assert "mysql_qps" not in collector._mysql_metrics("r", {"Questions": 100}, sampled_at=10)
    q = collector.subscribe("r")
    collector.unsubscribe("r", q)
    rates = collector._mysql_metrics("r", {"Questions": 200, "Innodb_deadlocks": 4}, sampled_at=12.5)
    assert rates["mysql_qps"] == 40 and rates["mysql_sample_interval_s"] == 2.5
    # A server counter reset is unknown for this interval, rather than an invented zero rate.
    assert "mysql_qps" not in collector._mysql_metrics("r", {"Questions": 1}, sampled_at=15)


def test_report_integrates_rates_without_counting_sql_rows_as_engine_samples(timing_db):
    write_history(timing_db, [[99, "Aggregated", 10, 100, 0, 5, 10, 20],
                             [110, "Aggregated", 12, 120, 0, 5, 10, 20]])
    sync_sql_history("timing")
    for ts, interval in [(100.5, 2.5), (103.5, 3)]:
        db.insert_metric(dict(run_id="timing", ts=ts, mysql_sample_interval_s=interval,
                              threads_running=1, slow_inc=2, lock_waits_inc=4,
                              tmp_disk_inc=1, bufpool_hit=0.99, mysql_qps=100))
    report = build_l0(db.get_run("timing"))
    assert report["mysql_slow_total"] == 11
    assert report["mysql_lock_waits_total"] == 22
    assert report["metrics_points"] == 2 and report["bufpool_hit_last"] == 0.99


@pytest.mark.parametrize("cancelled", [False, True])
def test_watcher_backfills_before_terminal_state_and_report(timing_db, monkeypatch, cancelled):
    write_history(timing_db, [[100, "Aggregated", 10, 100, 0, 5, 10, 20]])
    runner = LocustRunner()
    runner._procs["timing"] = SimpleNamespace(wait=lambda: 0)
    if cancelled:
        runner._cancel_requested.add("timing")
    seen = []

    def finalize(run, **kwargs):
        assert db.get_metrics("timing")[-1]["request_count"] == 100
        seen.append(run["status"])

    monkeypatch.setattr("app.services.runner.finalize_run", finalize)
    monkeypatch.setattr(runner, "_probe_explain_after_run", lambda *args: None)
    runner.set_on_finish(lambda _: seen.append("report"))
    runner._watch("timing")
    assert seen == ["cancelled" if cancelled else "finished", "report"]


def test_metrics_endpoint_declares_mixed_scopes(timing_db):
    payload = monitor.get_metrics("timing")
    assert payload["window_scope"] == "mixed"
    assert payload["metric_scopes"]["avg_ms"] == "cumulative"


def test_real_locust_child_flushes_terminal_history_and_summary(tmp_path):
    # A tiny synthetic Locust run verifies shutdown ordering without touching MySQL.
    project = str(Path(__file__).resolve().parents[2])
    locustfile = tmp_path / "synthetic.py"
    locustfile.write_text(
        f"import sys\nsys.path.insert(0, {project!r})\n"
        "from app.services.locust_history_writer import configure_csv_history\n"
        "configure_csv_history()\n"
        "from locust import User, task, constant\n"
        "class Synthetic(User):\n"
        "    wait_time = constant(0.02)\n"
        "    @task\n"
        "    def query(self):\n"
        "        self.environment.events.request.fire(request_type='SQL', name='sql_1', response_time=1, response_length=0)\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "child"
    proc = subprocess.run([sys.executable, "-m", "locust", "-f", str(locustfile),
                           "--headless", "-u", "1", "-r", "1", "-t", "1s",
                           "--csv", str(prefix), "--csv-full-history", "--only-summary"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    history = [r for r in HistoryReader().read(tmp_path / "child_stats_history.csv") if r["Name"] == "Aggregated"]
    with (tmp_path / "child_stats.csv").open(newline="") as handle:
        final = next(r for r in csv.DictReader(handle) if r["Name"] == "Aggregated")
    assert int(final["Request Count"]) > 0
    assert int(history[-1]["Total Request Count"]) == int(final["Request Count"])
    assert len(history) >= 2
