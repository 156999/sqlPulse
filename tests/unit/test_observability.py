import csv
import json

from app.config import settings
from app.services.observability import (
    build_event_timeline,
    build_snapshot,
    capacity_thresholds,
    read_exception_summary,
    read_final_metrics,
)


def _write(path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_snapshot_includes_user_start_failures_and_final_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    locust = tmp_path / "locust"
    locust.mkdir()
    _write(
        locust / "r_stats.csv",
        ["Name", "Request Count", "Failure Count", "Requests/s", "Average Response Time", "95%", "99%", "Max Response Time"],
        [{"Name": "Aggregated", "Request Count": "100", "Failure Count": "2", "Requests/s": "10", "Average Response Time": "20", "95%": "50", "99%": "90", "Max Response Time": "120"}],
    )
    _write(
        locust / "r_exceptions.csv",
        ["Count", "Message"],
        [{"Count": "7", "Message": "(1040, 'Too many connections')"}],
    )
    _write(
        locust / "r_failures.csv",
        ["Method", "Name", "Error", "Occurrences"],
        [{"Method": "SQL", "Name": "sql_1", "Error": "deadlock", "Occurrences": "2"}],
    )
    assert read_exception_summary("r")["exception_count"] == 7
    assert read_exception_summary("r")["top_exception"]["count"] == 7
    assert read_final_metrics("r")["request_error_rate"] == 0.02

    snapshot = build_snapshot({"id": "r", "status": "failed", "process_exit_code": 1})
    assert snapshot["error_kind"] == "database_rejected_connection"
    assert snapshot["quality"] == "partial"
    assert snapshot["final_metrics"]["scope"] == "cumulative"
    assert snapshot["request_failures"] == 2


def test_failed_runner_without_sql_errors_is_not_sql_unexecutable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "locust").mkdir()
    snapshot = build_snapshot({"id": "missing", "status": "failed", "process_exit_code": 1})
    assert snapshot["error_kind"] == "runner_process_failure"
    assert snapshot["quality"] == "not_usable"


def test_capacity_event_timeline_emits_alert_and_recovery():
    points = [
        {"ts": 10, "cpu_pct": 81, "p99_ms": 600},
        {"ts": 11, "cpu_pct": 90, "p99_ms": 700, "deadlocks_inc": 1},
        {"ts": 12, "cpu_pct": 50, "p99_ms": 100, "deadlocks_inc": 0},
    ]
    events = build_event_timeline(points)
    assert capacity_thresholds()["cpu_pct"]["threshold"] == 80.0
    assert any(e["metric"] == "cpu_pct" and e["event"] == "alert" for e in events)
    assert any(e["metric"] == "cpu_pct" and e["event"] == "recovery" for e in events)
    assert any(e["metric"] == "deadlocks_inc" and e["severity"] == "critical" for e in events)

