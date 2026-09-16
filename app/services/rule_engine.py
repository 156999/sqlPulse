"""L1 阈值规则引擎：纯函数，输入聚合指标 dict，输出建议列表。"""

DEFAULTS = {
    "p99_ms": 0, "slowest_sql_avg_ms": 0, "slowest_sql_id": "-",
    "threads_running_p95": 0, "max_connections": 0,
    "err_rate": 0, "qps_peak": 0, "concurrency": 1,
}

RULES = [
    ("p99_high", lambda m: m["p99_ms"] and m["p99_ms"] > 100, "warn",
     "P99 延迟超过 100ms",
     "P99={p99_ms:.0f}ms。检查是否有未走索引的查询，用 EXPLAIN 确认执行计划。"),
    ("slow_stmts", lambda m: m["slowest_sql_avg_ms"] and m["slowest_sql_avg_ms"] > 50, "warn",
     "存在明显慢语句",
     "语句 {slowest_sql_id} 平均 {slowest_sql_avg_ms:.0f}ms，为最慢语句，优先优化。"),
    ("conn_high", lambda m: m["max_connections"] and m["threads_running_p95"] / m["max_connections"] > 0.8, "warn",
     "连接数接近上限",
     "Threads_running P95 已达上限 80%+，检查连接池配置与长事务。"),
    ("err_rate", lambda m: m["err_rate"] and m["err_rate"] > 0.01, "error",
     "错误率超过 1%",
     "错误率 {err_rate:.1%}，检查 SQL 语法、锁超时与唯一键冲突。"),
    ("qps_low", lambda m: m["qps_peak"] and m["qps_peak"] < m["concurrency"] * 0.5, "info",
     "吞吐量低于并发预期",
     "QPS 峰值明显低于并发数，存在等待（锁/IO），查看锁等待指标。"),
]


def evaluate(metrics: dict) -> list:
    m = dict(DEFAULTS)
    m.update({k: v for k, v in metrics.items() if v is not None})
    out = []
    for rule_id, cond, level, title, tmpl in RULES:
        try:
            hit = bool(cond(m))
        except Exception:
            hit = False
        if hit:
            out.append({
                "rule_id": rule_id, "level": level, "title": title,
                "detail": tmpl.format(**m),
            })
    return out
