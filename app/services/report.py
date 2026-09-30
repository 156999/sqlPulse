import csv as csvmod
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pymysql
from loguru import logger

from app import db
from app.config import settings
from app.services.runner import parse_sql_tasks


def _num(row: dict, key: str, default=None):
    v = row.get(key)
    if v in (None, "", "N/A"):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_stats(run_id: str) -> tuple:
    """返回 (agg_row|None, [per_sql_row...])"""
    path = settings.data_dir / "locust" / f"{run_id}_stats.csv"
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csvmod.DictReader(f))
    except OSError:
        return None, []
    agg = [r for r in rows if r.get("Name") == "Aggregated"]
    per = [r for r in rows if r.get("Name") != "Aggregated"]
    return (agg[-1] if agg else None), per


def percentile(values: list, pct: float) -> Optional[float]:
    """线性插值分位。round() 取整在样本少时会明显偏移，
    L1 的 threads_running_p95 直接依赖它。"""
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    if len(vs) == 1:
        return vs[0]
    pos = pct * (len(vs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def query_max_connections(dsn: dict) -> Optional[int]:
    try:
        conn = pymysql.connect(
            host=dsn["host"], port=int(dsn["port"]), user=dsn["user"],
            password=dsn["password"], database=dsn["database"], connect_timeout=3,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW VARIABLES LIKE 'max_connections'")
                row = cur.fetchone()
                return int(row[1]) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("query max_connections failed: {}", e)
        return None


def build_l0(run: dict) -> dict:
    agg, per = read_stats(run["id"])
    metrics = db.get_metrics(run["id"])
    qps_values = [m["qps"] for m in metrics if m["qps"] is not None]
    tr_values = [m["threads_running"] for m in metrics]
    task_map = {t["sql_id"]: t for t in parse_sql_tasks(run["sql_content"])}

    def sql_preview(sql_id: str) -> str:
        t = task_map.get(sql_id)
        if not t:
            return ""
        return "; ".join(t["statements"]).replace("|", "\\|")[:80]

    per_sql = []
    for r in per:
        per_sql.append({
            "sql_id": r.get("Name") or "?",
            "sql": sql_preview(r.get("Name") or ""),
            "requests": int(_num(r, "Request Count", 0) or 0),
            "failures": int(_num(r, "Failure Count", 0) or 0),
            "avg_ms": _num(r, "Average Response Time"),
            "p50_ms": _num(r, "50%"),
            "p95_ms": _num(r, "95%"),
            "p99_ms": _num(r, "99%"),
            "max_ms": _num(r, "Max Response Time"),
            "qps": _num(r, "Requests/s"),
        })
    per_sql.sort(key=lambda x: -(x["avg_ms"] or 0))

    def col_sum(key):
        """返回 (合计, 是否有数据)。"真实为 0"（健康证据）与"没采到"
        （无法判断）必须区分 —— L1 的 B 层规则依赖这个区分。"""
        vals = [m[key] for m in metrics if m.get(key) is not None]
        return (sum(vals), True) if vals else (None, False)

    def col_max(key):
        vals = [m[key] for m in metrics if m.get(key) is not None]
        return max(vals) if vals else None

    def col_avg(key):
        vals = [m[key] for m in metrics if m.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    slow_total, slow_present = col_sum("slow_inc")
    lock_total, lock_present = col_sum("lock_waits_inc")
    tmp_total, tmp_present = col_sum("tmp_disk_inc")

    started = run.get("started_at")
    ended = run.get("ended_at")
    actual = None
    if started and ended:
        try:
            actual = max(0, int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()))
        except ValueError:
            actual = None

    l0 = {
        "run_id": run["id"],
        "name": run["name"],
        "status": run["status"],
        "concurrency": run["concurrency"],
        "spawn_rate": run["spawn_rate"],
        "duration_sec": run["duration_sec"],
        "actual_duration_sec": actual,
        "total_requests": int(_num(agg or {}, "Request Count", 0) or 0),
        "total_failures": int(_num(agg or {}, "Failure Count", 0) or 0),
        "avg_ms": _num(agg or {}, "Average Response Time"),
        "p50_ms": _num(agg or {}, "50%"),
        "p95_ms": _num(agg or {}, "95%"),
        "p99_ms": _num(agg or {}, "99%"),
        "max_ms": _num(agg or {}, "Max Response Time"),
        "qps_avg": (sum(qps_values) / len(qps_values)) if qps_values else None,
        "qps_peak": max(qps_values) if qps_values else None,
        "err_rate": None,
        "threads_running_p95": percentile(tr_values, 0.95),
        "mysql_slow_total": slow_total,
        "mysql_slow_present": slow_present,
        "mysql_lock_waits_total": lock_total,
        "mysql_lock_waits_present": lock_present,
        "tmp_disk_total": tmp_total,
        "tmp_disk_present": tmp_present,
        "lock_waits_rate_max": col_max("lock_waits_inc"),
        "mysql_qps_avg": col_avg("mysql_qps"),
        "threads_connected_max": col_max("threads_connected"),
        "bufpool_hit_last": metrics[-1]["bufpool_hit"] if metrics else None,
        "metrics_points": len(metrics),
        "per_sql": per_sql,
    }
    if l0["total_requests"]:
        l0["err_rate"] = l0["total_failures"] / l0["total_requests"]
    return l0


def render_markdown(run: dict, l0: dict, l1) -> str:
    """完整报告 Markdown。L0 段落 + L1 诊断统一由 l1/render 渲染，
    旧 list 形状的 l1（历史数据）会被 normalize 成兼容视图。"""
    from app.services.l1 import render as l1_render
    return l1_render.render_markdown(run, l0, l1)


def generate_report(run_id: str) -> Optional[dict]:
    run = db.get_run(run_id)
    if not run or run["status"] in ("pending", "running"):
        return None
    l0 = build_l0(run)
    dsn = json.loads(run["db_dsn_json"])
    max_conn = query_max_connections(dsn)

    from app.services import explain_probe
    from app.services.collector import read_per_sql_history
    from app.services.l1 import build as l1_build
    from app.services.l1 import evidence as l1_evidence
    from app.services.l1 import render as l1_render
    from app.services.l1.thresholds import Thresholds

    plan_artifact = explain_probe.load_artifact(run_id)
    series = read_per_sql_history(
        settings.data_dir / "locust" / f"{run_id}_stats_history.csv")
    th = Thresholds.from_settings(settings)

    inp = l1_evidence.collect(
        run=run, l0=l0, series=series, plan_artifact=plan_artifact,
        target={"max_connections": max_conn,
                "server_version": (plan_artifact or {}).get("server_version")},
    )
    l1_report = l1_build(inp, th)
    l1_dict = l1_report.to_json()

    out_dir = settings.reports_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    md = l1_render.render_markdown(run, l0, l1_dict)
    md_path = out_dir / "report.md"
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps({"l0": l0, "l1": l1_dict}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    db.save_report(run_id, l0, l1_dict, str(md_path))
    logger.info("report generated for {}: verdict={}, {} findings",
                run_id, l1_report.verdict.get("level"), len(l1_report.findings))
    return db.get_report(run_id)
