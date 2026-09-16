import csv as csvmod
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pymysql
from loguru import logger

from app import db
from app.config import settings
from app.services.rule_engine import evaluate
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
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    idx = min(len(vs) - 1, max(0, round(pct * (len(vs) - 1))))
    return vs[idx]


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
        return sum(m[key] for m in metrics if m.get(key) is not None) or None

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
        "mysql_slow_total": col_sum("slow_inc"),
        "mysql_lock_waits_total": col_sum("lock_waits_inc"),
        "per_sql": per_sql,
    }
    if l0["total_requests"]:
        l0["err_rate"] = l0["total_failures"] / l0["total_requests"]
    return l0


def render_markdown(run: dict, l0: dict, l1: list) -> str:
    def fmt(v, suffix=""):
        return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else "-"

    lines = [f"# 压测报告 {run['id']}", ""]
    lines += ["## 任务参数", "",
              f"- 任务名：{run['name']}",
              f"- 状态：{run['status']}",
              f"- 并发数：{run['concurrency']}（spawn {run['spawn_rate']}/s）",
              f"- 计划时长：{run['duration_sec']}s（实际 {l0.get('actual_duration_sec') or '-'}s）",
              f"- 开始时间：{run.get('started_at') or '-'}",
              f"- 结束时间：{run.get('ended_at') or '-'}",
              ""]
    if run.get("error_msg"):
        lines += [f"> 错误信息：{run['error_msg']}", ""]

    err_rate_str = f"{l0['err_rate']:.2%}" if l0["err_rate"] is not None else "-"
    lines += ["## L0 指标总览", "",
              "| 指标 | 数值 |", "|---|---|",
              f"| 总请求数 | {l0['total_requests']} |",
              f"| 总失败数 | {l0['total_failures']} |",
              f"| 错误率 | {err_rate_str} |"]
    lines += [f"| QPS 均值 / 峰值 | {fmt(l0['qps_avg'])} / {fmt(l0['qps_peak'])} |",
              f"| 平均延迟 | {fmt(l0['avg_ms'], 'ms')} |",
              f"| P50 / P95 / P99 | {fmt(l0['p50_ms'], 'ms')} / {fmt(l0['p95_ms'], 'ms')} / {fmt(l0['p99_ms'], 'ms')} |",
              f"| 最大延迟 | {fmt(l0['max_ms'], 'ms')} |",
              f"| Threads_running P95 | {l0['threads_running_p95'] if l0['threads_running_p95'] is not None else '-'} |",
              f"| MySQL 慢查询（增量） | {l0['mysql_slow_total'] if l0['mysql_slow_total'] is not None else '-'} 条 |",
              f"| MySQL 行锁等待（增量） | {l0['mysql_lock_waits_total'] if l0['mysql_lock_waits_total'] is not None else '-'} 次 |",
              ""]

    lines += ["## 分语句统计", "", "| 语句 | 内容 | 请求数 | 失败数 | 平均 | P95 | P99 | 最大 | QPS |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s in l0["per_sql"]:
        lines.append(
            f"| {s['sql_id']} | {s.get('sql', '')} | {s['requests']} | {s['failures']} | {fmt(s['avg_ms'], 'ms')} | "
            f"{fmt(s['p95_ms'], 'ms')} | {fmt(s['p99_ms'], 'ms')} | {fmt(s['max_ms'], 'ms')} | {fmt(s['qps'])} |"
        )
    lines.append("")

    lines += ["## L1 规则建议（本地规则报告，未配置 LLM）", ""]
    if l1:
        for a in l1:
            lines.append(f"- **[{a['level'].upper()}] {a['title']}**：{a['detail']}")
    else:
        lines.append("- 无命中规则，各项指标在阈值内。")
    lines += ["", "---", "*SQL Pulse MVP · L2 LLM 诊断段落（预留）*"]
    return "\n".join(lines) + "\n"


def generate_report(run_id: str) -> Optional[dict]:
    run = db.get_run(run_id)
    if not run or run["status"] in ("pending", "running"):
        return None
    l0 = build_l0(run)
    dsn = json.loads(run["db_dsn_json"])
    max_conn = query_max_connections(dsn)

    slowest = l0["per_sql"][0] if l0["per_sql"] else {}
    metrics_in = {
        **l0,
        "slowest_sql_avg_ms": slowest.get("avg_ms"),
        "slowest_sql_id": slowest.get("sql_id", "-"),
        "max_connections": max_conn,
    }
    l1 = evaluate(metrics_in)

    out_dir = settings.reports_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(run, l0, l1)
    md_path = out_dir / "report.md"
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(l0, ensure_ascii=False, indent=2), encoding="utf-8")

    db.save_report(run_id, l0, l1, str(md_path))
    logger.info("report generated for {}: {} suggestions", run_id, len(l1))
    return db.get_report(run_id)
