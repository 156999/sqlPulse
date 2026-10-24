"""L1 单测共用的输入工厂。

全部内联 dict，不读文件、不连库（与 test_explain_view.py 同风格）。
默认值是一份"健康基线"：三层规则全不命中，X8 需要的采集条件全满足。
"""
from app.services.l1.contract import L1Input


def make_ledger(**kw) -> dict:
    base = {
        "run_id": "r-test", "name": "test run", "status": "success",
        "concurrency": 10, "spawn_rate": 1,
        "duration_sec": 60, "actual_duration_sec": 60,
        "total_requests": 10000, "total_failures": 0, "err_rate": 0.0,
        "avg_ms": 12.0, "p50_ms": 10.0, "p95_ms": 20.0, "p99_ms": 30.0,
        "max_ms": 50.0, "qps_avg": 100.0, "qps_peak": 120.0,
        "threads_running_p95": 5,
        "mysql_slow_total": 0, "mysql_slow_present": True,
        "mysql_lock_waits_total": 0, "mysql_lock_waits_present": True,
        "tmp_disk_total": 0, "tmp_disk_present": True,
        "lock_waits_rate_max": 0.0, "mysql_qps_avg": 100.0,
        "threads_connected_max": 12, "bufpool_hit_last": 0.99,
        "metrics_points": 60,
        "per_sql": [{
            "sql_id": "sql_1", "sql": "SELECT * FROM t WHERE id = 1",
            "requests": 10000, "failures": 0,
            "avg_ms": 12.0, "p50_ms": 10.0, "p95_ms": 20.0,
            "p99_ms": 30.0, "max_ms": 50.0, "qps": 100.0,
        }],
    }
    base.update(kw)
    return base


def make_engine(**kw) -> dict:
    base = {
        "points": 60,
        "slow_total": 0, "slow_present": True,
        "lock_waits_total": 0, "lock_waits_present": True,
        "lock_waits_rate_max": 0.0,
        "tmp_disk_total": 0, "tmp_disk_present": True,
        "bufpool_hit": 0.99,
        "threads_running_p95": 5,
        "threads_connected_max": 12,
        "mysql_qps_avg": 100.0,
    }
    base.update(kw)
    return base


def make_plan(**kw) -> dict:
    """健康计划：1 个 task、2 条语句、无坏味道 finding。"""
    base = {
        "run_id": "r-test", "probe_version": 3, "phase": "post_run",
        "ok": True, "error": None, "server_version": "8.0.36",
        "summary": {
            "tasks": 1, "statements": 2, "probed": 2,
            "skipped_not_explainable": 0, "dropped_by_limit": 0, "errors": 0,
            "findings": 0, "by_code": {}, "worst_level": None,
            "max_rows": 100, "max_rows_ref": {"sql_id": "sql_1", "index": 0},
            "elapsed_ms": 12,
        },
        "tasks": [{
            "sql_id": "sql_1", "weight": 1,
            "statements": [
                {"index": 0, "original": "SELECT * FROM t WHERE id = 1",
                 "filled": "SELECT * FROM t WHERE id = 1", "parameters": [],
                 "ok": True, "plan": [{"table": "t", "type": "const", "key": "PRIMARY",
                                       "rows": 1, "Extra": None}],
                 "max_rows": 1, "findings": []},
                {"index": 1, "original": "SELECT 1", "filled": "SELECT 1",
                 "parameters": [], "ok": True, "plan": [], "max_rows": 1,
                 "findings": []},
            ],
        }],
    }
    base.update(kw)
    return base


def plan_with_findings(code: str, table: str = "orders", sql_id: str = "sql_1") -> dict:
    """给 sql_1 的第 1 条语句加一个坏味道 finding，并同步 summary.by_code。"""
    plan = make_plan()
    plan["tasks"][0]["statements"][0]["findings"].append(
        {"code": code, "level": "warn", "table": table, "detail": f"{code} on {table}"})
    plan["summary"]["by_code"] = {code: 1}
    plan["summary"]["findings"] = 1
    plan["summary"]["worst_level"] = "warn"
    plan["tasks"][0]["sql_id"] = sql_id
    return plan


def make_series(n: int = 10, rps: float = 100.0, p99_ms: float = 30.0) -> dict:
    return {"sql_1": [
        {"ts": 1700000000 + i, "rps": rps, "fails": 0.0,
         "avg_ms": 12.0, "p95_ms": 20.0, "p99_ms": p99_ms}
        for i in range(n)
    ]}


def make_input(**kw) -> L1Input:
    defaults = dict(
        run={"id": "r-test", "name": "test run", "status": "success",
             "concurrency": 10, "spawn_rate": 1, "duration_sec": 60,
             "error_msg": None},
        ledger=make_ledger(),
        series=make_series(),
        engine=make_engine(),
        plan=make_plan(),
        target={"max_connections": 151, "server_version": "8.0.36"},
        tasks=[],
    )
    defaults.update(kw)
    return L1Input(**defaults)


def healthy_comp(**kw) -> dict:
    """与 make_input 基线一致的完整 completeness。"""
    base = {
        "plan_available": True, "plan_reason": "",
        "plan_probed": 2, "plan_statements": 2,
        "plan_truncated": False, "plan_truncated_reason": None,
        "metrics_points": 60, "metrics_sufficient": True,
        "series_available": True,
        "total_requests": 10000, "sample_sufficient": True,
        "duration_ratio": 1.0, "duration_sufficient": True,
    }
    base.update(kw)
    return base
