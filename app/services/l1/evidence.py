"""把 A/A'/B/C/D/E 六路数据组装成 L1Input，并判定采集完整性。

本模块是 L1 里唯一接触外部数据形状的地方；不读文件、不连库——
原始数据由调用方（report.generate_report）读好传进来，这样
collect() 与 build() 都能离线单测。
"""
from app.services.l1.contract import L1Input


def _engine_view(l0: dict) -> dict:
    """l0 里的引擎侧标量 → B 层输入。present=False 表示"没采到"，
    与"真实为 0"（健康证据）严格区分 —— 见 l1_report_design.md §2.3 P1。"""
    return {
        "points": l0.get("metrics_points") or 0,
        "slow_total": l0.get("mysql_slow_total"),
        "slow_present": bool(l0.get("mysql_slow_present")),
        "lock_waits_total": l0.get("mysql_lock_waits_total"),
        "lock_waits_present": bool(l0.get("mysql_lock_waits_present")),
        "lock_waits_rate_max": l0.get("lock_waits_rate_max"),
        "tmp_disk_total": l0.get("tmp_disk_total"),
        "tmp_disk_present": bool(l0.get("tmp_disk_present")),
        "bufpool_hit": l0.get("bufpool_hit_last"),
        "threads_running_p95": l0.get("threads_running_p95"),
        "threads_connected_max": l0.get("threads_connected_max"),
        "mysql_qps_avg": l0.get("mysql_qps_avg"),
    }


def collect(run: dict, l0: dict, series: dict, plan_artifact, target: dict) -> L1Input:
    return L1Input(
        run=run or {},
        ledger=l0 or {},
        series=series or {},
        engine=_engine_view(l0 or {}),
        plan=plan_artifact,
        target=target or {},
        tasks=_tasks_of(run or {}),
    )


def _tasks_of(run: dict) -> list:
    # 延迟 import：runner 会带起 explain_probe → sql_executor 整条依赖链
    from app.services.runner import resolve_tasks
    try:
        return resolve_tasks(run)
    except Exception:
        return []


def completeness(inp: L1Input, th) -> dict:
    """采集完整性判定。供 verdict（置信度）、render（完整性章节）共用。"""
    led = inp.ledger or {}
    plan = inp.plan or {}
    summary = plan.get("summary") or {}
    duration = led.get("duration_sec") or 0
    actual = led.get("actual_duration_sec") or 0
    ratio = (actual / duration) if duration else 0.0
    points = inp.engine.get("points") or 0
    total_req = led.get("total_requests") or 0
    return {
        "plan_available": bool(inp.plan),
        "plan_reason": plan_reason(inp.plan),
        "plan_probed": summary.get("probed"),
        "plan_statements": summary.get("statements"),
        "plan_truncated": plan.get("truncated"),
        "plan_truncated_reason": plan.get("truncated_reason"),
        "metrics_points": points,
        "metrics_sufficient": points >= th.min_points,
        "series_available": bool(inp.series),
        "total_requests": total_req,
        "sample_sufficient": total_req >= th.min_requests,
        "duration_ratio": round(ratio, 3),
        "duration_sufficient": ratio >= th.duration_ratio,
    }


def plan_reason(plan) -> str:
    """C10 的三态缺失原因，措辞与 explain_view.build_view() 的空态一致。"""
    if plan:
        return ""
    from app.config import settings
    if not settings.explain_probe_enabled:
        return "采集被配置关闭（EXPLAIN_PROBE_ENABLED=false）"
    return ("压测进程异常退出导致收尾没走到（服务重启、进程被强杀，孤儿 run 会被直接标 failed）、"
            "或 data/explain/ 下的产物已被清理")
