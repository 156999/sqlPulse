"""Run observability snapshots.

Locust exposes request failures in stats.csv, while failures during user
initialisation are written to exceptions.csv.  Keeping those sources together
prevents the monitor from reporting a misleading zero error rate.
"""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings


def capacity_thresholds() -> dict:
    """Thresholds used by the capacity event timeline and alert cards."""
    return {
        "cpu_pct": {"threshold": settings.capacity_cpu_warn_pct, "unit": "%", "severity": "warning", "title": "CPU 使用率过高"},
        "memory_pct": {"threshold": settings.capacity_memory_warn_pct, "unit": "%", "severity": "warning", "title": "内存使用率过高"},
        "disk_used_pct": {"threshold": settings.capacity_disk_warn_pct, "unit": "%", "severity": "warning", "title": "磁盘使用率过高"},
        "connection_usage_pct": {"threshold": settings.capacity_connection_warn_pct, "unit": "%", "severity": "warning", "title": "连接容量使用率过高"},
        "lock_wait_ms_inc": {"threshold": settings.capacity_lock_wait_ms_warn, "unit": "ms/s", "severity": "critical", "title": "锁等待时间过高"},
        "deadlocks_inc": {"threshold": settings.capacity_deadlocks_warn, "unit": "次/s", "severity": "critical", "title": "检测到死锁"},
        "innodb_log_waits_inc": {"threshold": settings.capacity_log_waits_warn, "unit": "次/s", "severity": "critical", "title": "InnoDB 日志等待"},
        "p99_ms": {"threshold": settings.capacity_p99_warn_ms, "unit": "ms", "severity": "warning", "title": "P99 延迟超过阈值"},
    }


def build_event_timeline(points: list[dict]) -> list[dict]:
    """Create transition events from sampled metrics, suppressing duplicates."""
    rules = capacity_thresholds()
    active = {metric: False for metric in rules}
    events = []
    for point in sorted(points or [], key=lambda item: item.get("ts") or 0):
        ts = point.get("ts")
        for metric, rule in rules.items():
            value = point.get(metric)
            if value is None:
                continue
            breached = float(value) >= float(rule["threshold"])
            if breached and not active[metric]:
                events.append({
                    "ts": ts,
                    "event": "alert",
                    "severity": rule["severity"],
                    "metric": metric,
                    "title": rule["title"],
                    "value": value,
                    "threshold": rule["threshold"],
                    "unit": rule["unit"],
                })
            elif not breached and active[metric]:
                events.append({
                    "ts": ts,
                    "event": "recovery",
                    "severity": "info",
                    "metric": metric,
                    "title": f"{rule['title']}恢复",
                    "value": value,
                    "threshold": rule["threshold"],
                    "unit": rule["unit"],
                })
            active[metric] = breached
    return events


def _read_rows(path: Path) -> list[dict]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _num(row: dict, keys: list[str], default=None):
    for key in keys:
        value = row.get(key)
        if value in (None, "", "N/A"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _int(row: dict, keys: list[str], default: int = 0) -> int:
    value = _num(row, keys, default)
    return int(value or 0)


def _locust_path(run_id: str, suffix: str) -> Path:
    return settings.data_dir / "locust" / f"{run_id}_{suffix}.csv"


def read_exception_summary(run_id: str) -> dict:
    """Return Locust exception/failure counts and the most common exception."""
    exception_rows = _read_rows(_locust_path(run_id, "exceptions"))
    exception_count = sum(_int(row, ["Count", "Occurrences"]) for row in exception_rows)
    top = None
    if exception_rows:
        ranked = sorted(
            (
                _int(row, ["Count", "Occurrences"]),
                (row.get("Message") or row.get("Error") or "未知异常").strip(),
            )
            for row in exception_rows
        )
        count, message = ranked[-1]
        if count:
            top = {"message": message, "count": count}

    failure_rows = _read_rows(_locust_path(run_id, "failures"))
    failure_events = sum(_int(row, ["Occurrences", "Count"]) for row in failure_rows)
    return {
        "exception_count": exception_count,
        "user_start_failures": exception_count,
        "failure_events": failure_events,
        "top_exception": top,
    }


def read_final_metrics(run_id: str) -> Optional[dict]:
    rows = [
        row for row in _read_rows(_locust_path(run_id, "stats"))
        if row.get("Name") == "Aggregated"
    ]
    if not rows:
        return None
    row = rows[-1]
    request_count = _int(row, ["Request Count", "Total Request Count"])
    request_failures = _int(row, ["Failure Count", "Total Failure Count"])
    return {
        "scope": "cumulative",
        "request_count": request_count,
        "request_failures": request_failures,
        "request_error_rate": request_failures / request_count if request_count else None,
        "qps_avg": _num(row, ["Requests/s", "Current RPS"]),
        "avg_ms": _num(row, ["Average Response Time", "Total Average Response Time"]),
        "p95_ms": _num(row, ["95%"]),
        "p99_ms": _num(row, ["99%"]),
        "max_ms": _num(row, ["Max Response Time", "Total Max Response Time"]),
    }


def classify_run(
    run: dict,
    final_metrics: Optional[dict],
    exceptions: dict,
) -> dict:
    status = run.get("status")
    exit_code = run.get("process_exit_code")
    request_count = (final_metrics or {}).get("request_count", 0)
    request_failures = (final_metrics or {}).get("request_failures", 0)
    error_rate = (final_metrics or {}).get("request_error_rate")
    top_message = ((exceptions.get("top_exception") or {}).get("message") or "").lower()

    if status == "cancelled":
        error_kind = "user_cancelled"
    elif exceptions.get("exception_count") and "too many connections" in top_message:
        error_kind = "database_rejected_connection"
    elif error_rate is not None and error_rate >= 0.99:
        error_kind = "sql_request_failure"
    elif status == "failed" and exit_code not in (None, 0):
        error_kind = "runner_process_failure"
    elif request_failures:
        error_kind = "sql_request_failure"
    else:
        error_kind = None

    if status in ("pending", "running"):
        quality = "pending"
        reason = "压测尚未结束，最终累计指标尚未生成"
    elif request_count == 0 and status in ("failed", "cancelled"):
        quality = "not_usable"
        reason = "没有成功完成的请求，无法形成性能结论"
    elif (
        exceptions.get("exception_count")
        or exit_code not in (None, 0)
        or request_failures
    ):
        quality = "partial"
        if error_kind == "database_rejected_connection":
            reason = f"{exceptions['exception_count']} 个用户初始化连接失败，性能数据仅覆盖成功连接的用户"
        elif error_kind == "sql_request_failure":
            reason = "存在请求执行失败，性能数据需要结合失败样本解释"
        elif error_kind == "runner_process_failure":
            reason = f"压测进程异常退出（退出码 {exit_code}），结果可能不完整"
        elif error_kind == "user_cancelled":
            reason = "任务被主动停止，结果只覆盖已运行时段"
        else:
            reason = "存在异常或失败样本，结果不代表完整容量"
    elif status == "finished":
        quality = "valid"
        reason = "请求、异常和压测进程均未报告失败"
    else:
        quality = "unknown"
        reason = "缺少足够的任务状态信息"

    return {
        "quality": quality,
        "quality_reason": reason,
        "error_kind": error_kind,
        "error_state": "error" if error_kind else ("pending" if quality == "pending" else "ok"),
    }


def build_snapshot(run: dict, metrics: Optional[list[dict]] = None) -> dict:
    final_metrics = read_final_metrics(run["id"])
    exceptions = read_exception_summary(run["id"])
    points = metrics or []
    latest = points[-1] if points else {}
    snapshot = {
        **exceptions,
        "metrics_points": len(points),
        "latest_capacity": {
            key: latest.get(key) for key in (
                "threads_connected", "max_connections", "active_users", "connection_usage_pct",
            )
        },
        "request_count": (final_metrics or {}).get("request_count", 0),
        "request_failures": (final_metrics or {}).get("request_failures", 0),
        "request_error_rate": (final_metrics or {}).get("request_error_rate"),
        "final_metrics": final_metrics,
        "process_exit_code": run.get("process_exit_code"),
    }
    snapshot.update(classify_run(run, final_metrics, exceptions))
    return snapshot


def finalize_run(run: dict, process_exit_code: Optional[int] = None) -> dict:
    """Persist the final observability snapshot after Locust closes its files."""
    from app import db

    if process_exit_code is not None:
        run = {**run, "process_exit_code": process_exit_code}
    snapshot = build_snapshot(run, db.get_metrics(run["id"]))
    db.update_run(
        run["id"],
        {
            "process_exit_code": process_exit_code if process_exit_code is not None else run.get("process_exit_code"),
            "final_metrics_json": json.dumps(snapshot.get("final_metrics"), ensure_ascii=False),
            "observability_json": json.dumps(snapshot, ensure_ascii=False),
            "finalized_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return snapshot
