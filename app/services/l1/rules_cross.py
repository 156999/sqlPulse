"""X 层 · 交叉归因（L1 的核心价值）+ 去重 + 总评。

X 规则要求 ≥2 个来源同时命中才输出"根因"，每条带证据三联
（账本 + 引擎 + 计划）。命中后吸收构成它的基础规则（CONSUMES），
避免"同一件事说三遍"。
"""
from app.services.l1.contract import Action, Evidence, Finding, META_RULES
from app.services.l1.rules_plan import plan_indexes, plan_healthy

# X 规则 → 被它吸收的基础规则（从 findings 摘除，内容保留在 X 的 evidence 里）
CONSUMES = {
    "X1_root_index_missing": {"A5_slow_statement", "A6_slow_statement_severe",
                              "C1_plan_table_scan", "C2_plan_index_ignored",
                              "C3_plan_full_index_scan"},
    "X2_root_lock_contention": {"B2_lock_waits", "B3_lock_waits_heavy",
                                "A9_throughput_below_expectation"},
    "X3_root_tmp_disk": {"A5_slow_statement", "A6_slow_statement_severe",
                         "B4_tmp_disk_tables", "C4_plan_filesort", "C5_plan_temporary"},
    "X4_root_buffer_pool": {"B5_bufpool_hit_low", "B6_bufpool_hit_bad",
                            "C7_plan_large_scan"},
    "X5_root_write_path": {"B2_lock_waits", "B3_lock_waits_heavy"},
    "X6_root_conn_saturation": {"B7_conn_near_limit", "B8_threads_exceed_concurrency",
                                "A9_throughput_below_expectation"},
}


def apply_cross(inp, th, base_findings: list, comp: dict) -> list:
    hits = {f.rule_id for f in base_findings}
    out = []
    out += _x1(inp, th, hits)
    out += _x2(inp, th, hits)
    out += _x3(inp, th, hits)
    out += _x4(inp, th, hits)
    out += _x5(inp, th, hits)
    out += _x6(inp, th, hits)
    out += _x7(inp)
    out += _x8(base_findings, comp)
    return out


def dedup(base: list, cross: list) -> list:
    absorbed = set()
    for x in cross:
        absorbed |= CONSUMES.get(x.rule_id, set())
    kept = [f for f in base if f.rule_id not in absorbed]
    return kept + cross


def _fmt(v, nd=0):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v)


def _x1(inp, th, hits):
    """慢语句 ∧ 该语句计划有扫描类问题 → 缺索引 / 索引失效。"""
    slow = [s for s in (inp.ledger.get("per_sql") or [])
            if (s.get("avg_ms") or 0) > th.sql_avg_ms
            or (s.get("p99_ms") or 0) > th.sql_p99_ms]
    if not slow:
        return []
    idx = plan_indexes(inp.plan or {})
    scan_codes = ("table_scan", "index_ignored", "full_index_scan")
    slow_ids = {s["sql_id"] for s in slow}
    bad = [i for c in scan_codes for i in idx.get(c, []) if i["sql_id"] in slow_ids]
    if not bad:
        return []
    worst = max(slow, key=lambda s: s.get("p99_ms") or 0)
    sql_id = worst["sql_id"]
    tables = sorted({i["table"] for i in bad if i["table"]})
    codes = sorted({c for c in scan_codes if idx.get(c)})
    ev = [
        Evidence("ledger", f"{sql_id} 延迟",
                 f"平均 {_fmt(worst.get('avg_ms'), 1)}ms",
                 f"P95 {_fmt(worst.get('p95_ms'), 1)}ms / P99 {_fmt(worst.get('p99_ms'), 1)}ms"),
        Evidence("plan", "计划问题", ", ".join(codes),
                 f"涉及表：{', '.join(tables) or '-'}"),
    ]
    eng = inp.engine
    if eng.get("lock_waits_present") and (eng.get("lock_waits_total") or 0) > 0:
        ev.append(Evidence("engine", "行锁等待增量", _fmt(eng["lock_waits_total"]),
                           "与扫描放大同源"))
    return [Finding(
        rule_id="X1_root_index_missing", layer="cross", severity="warn",
        title=f"{sql_id} 慢的根因：{', '.join(tables) or '目标表'} 未有效使用索引",
        scope={"sql_id": sql_id, "table": tables[0] if tables else None},
        evidence=ev,
        root_cause="该语句既慢、执行计划又存在全表扫描 / 索引未被选择，两者互为因果",
        actions=[Action("sql", "检查 WHERE / JOIN 谓词是否对索引列做函数或类型转换"),
                 Action("index", f"按 {', '.join(tables) or '目标表'} 的 WHERE / JOIN 列评估索引"),
                 Action("benchmark", "加索引后用同一 SQL 复跑，对比 P99")],
    )]


def _x2(inp, th, hits):
    """锁等待 ∧ 性能受抑 ∧ 计划无扫描类问题 → 行锁竞争。"""
    lock_hit = bool({"B2_lock_waits", "B3_lock_waits_heavy"} & hits)
    perf_hit = bool({"A5_slow_statement", "A6_slow_statement_severe",
                     "A9_throughput_below_expectation"} & hits)
    if not (lock_hit and perf_hit):
        return []
    idx = plan_indexes(inp.plan or {})
    if any(idx.get(c) for c in ("table_scan", "index_ignored")):
        return []   # 读路径有问题时优先归因 X1，这里让路
    eng = inp.engine
    ev = [
        Evidence("engine", "行锁等待增量", _fmt(eng.get("lock_waits_total")),
                 f"峰值 {_fmt(eng.get('lock_waits_rate_max'), 1)}/s"),
    ]
    slowest = (inp.ledger.get("per_sql") or [{}])[0]
    if slowest:
        ev.append(Evidence("ledger", f"最慢语句 {slowest.get('sql_id')}",
                           f"平均 {_fmt(slowest.get('avg_ms'), 1)}ms", ""))
    if inp.plan:
        ev.append(Evidence("plan", "读路径计划", "无明显扫描类问题",
                           "排除索引问题后，锁竞争是更可能的根因"))
    return [Finding(
        rule_id="X2_root_lock_contention", layer="cross", severity="warn",
        title="根因判定：行锁竞争",
        scope={"sql_id": slowest.get("sql_id")} if slowest else {},
        evidence=ev,
        root_cause="锁等待与性能受限同时出现、而读路径计划健康：竞争来自写事务或热点行",
        actions=[Action("sql", "缩短事务、拆批写入、避开热点行"),
                 Action("config", "确认隔离级别与锁超时配置"),
                 Action("benchmark", "调整并发复现并验证改善")],
    )]


def _x3(inp, th, hits):
    """磁盘临时表 ∧ filesort/temporary ∧ 慢 → 排序/分组无索引落盘。"""
    if not ({"B4_tmp_disk_tables"} & hits and
            {"C4_plan_filesort", "C5_plan_temporary"} & hits):
        return []
    idx = plan_indexes(inp.plan or {})
    ev = [
        Evidence("engine", "磁盘临时表增量", _fmt(inp.engine.get("tmp_disk_total")), ""),
        Evidence("plan", "计划信号", "filesort / temporary",
                 ", ".join(sorted({i["table"] for c in ("filesort", "temporary")
                                   for i in idx.get(c, []) if i["table"]})) or "-"),
    ]
    return [Finding(
        rule_id="X3_root_tmp_disk", layer="cross", severity="warn",
        title="根因判定：排序/分组无索引，结果落盘",
        evidence=ev,
        root_cause="GROUP BY / ORDER BY 无索引支撑，内存放不下转磁盘临时表",
        actions=[Action("index", "为 ORDER BY / GROUP BY 列建索引"),
                 Action("sql", "减少排序/分组的数据量与 DISTINCT")],
    )]


def _x4(inp, th, hits):
    """缓冲池命中率低 ∧ 长尾/慢 ∧ 大扫描 → 缓冲池不足或大范围扫描。"""
    if not ({"B5_bufpool_hit_low", "B6_bufpool_hit_bad"} & hits and
            {"A1_p99_high", "A4_long_tail"} & hits and
            {"C1_plan_table_scan", "C7_plan_large_scan"} & hits):
        return []
    return [Finding(
        rule_id="X4_root_buffer_pool", layer="cross", severity="warn",
        title="根因判定：缓冲池不足 / 大范围冷读",
        evidence=[
            Evidence("engine", "缓冲池命中率", f"{(inp.engine.get('bufpool_hit') or 0):.1%}", "累计口径"),
            Evidence("ledger", "汇总 P99", f"{_fmt(inp.ledger.get('p99_ms'), 1)}ms", ""),
            Evidence("plan", "扫描量", _fmt((inp.plan or {}).get("summary", {}).get("max_rows")),
                     "优化器估算"),
        ],
        root_cause="大范围扫描带来冷读，工作集超出缓冲池：物理 IO 主导延迟",
        actions=[Action("config", "评估 innodb_buffer_pool_size"),
                 Action("index", "收窄扫描范围，让工作集进得了内存")],
    )]


def _x5(inp, th, hits):
    """锁等待 ∧ 读路径完全健康 ∧ 账本无慢语句/吞吐异常 → 纯写路径竞争。"""
    if not ({"B2_lock_waits", "B3_lock_waits_heavy"} & hits):
        return []
    if not inp.plan or not plan_healthy(inp.plan):
        return []
    if {"A5_slow_statement", "A6_slow_statement_severe",
        "A9_throughput_below_expectation"} & hits:
        return []   # 读路径受抑时归 X2
    return [Finding(
        rule_id="X5_root_write_path", layer="cross", severity="info",
        title="锁竞争集中在写路径（读路径计划健康）",
        evidence=[
            Evidence("engine", "行锁等待增量", _fmt(inp.engine.get("lock_waits_total")), ""),
            Evidence("plan", "读路径计划", "健康（无坏味道 finding）", ""),
        ],
        root_cause="EXPLAIN 只解释读路径：锁竞争来自写入侧的事务，与查询计划无关",
        actions=[Action("sql", "检查写语句的事务边界，缩短持锁时间"),
                 Action("benchmark", "用 lock_waits 时间窗定位竞争时段")],
    )]


def _x6(inp, th, hits):
    """连接饱和 ∧ 吞吐低于预期 ∧ 计划健康 → 连接/线程饱和。"""
    if not ({"B7_conn_near_limit", "B8_threads_exceed_concurrency"} & hits and
            {"A9_throughput_below_expectation"} & hits):
        return []
    return [Finding(
        rule_id="X6_root_conn_saturation", layer="cross", severity="warn",
        title="根因判定：连接 / 线程饱和",
        evidence=[
            Evidence("engine", "Threads_running P95",
                     _fmt(inp.engine.get("threads_running_p95")),
                     f"max_connections = {(inp.target or {}).get('max_connections') or '-'}"),
            Evidence("ledger", "QPS 峰值", _fmt(inp.ledger.get("qps_peak"), 1),
                     f"并发 {inp.run.get('concurrency')}"),
        ],
        root_cause="执行中的 SQL 堆积占满连接：吞吐被连接数而非 SQL 本身限制",
        actions=[Action("config", "核对连接池与 max_connections 配置"),
                 Action("benchmark", "确认并发设定与连接预算匹配")],
    )]


def _x7(inp):
    """SQL 本身不可执行 —— 不是性能问题，压测无效。"""
    led = inp.ledger
    err = led.get("err_rate")
    failed = inp.run.get("status") == "failed"
    if (err is not None and err >= 0.99) or failed:
        ev = []
        if err is not None:
            ev.append(Evidence("ledger", "错误率", f"{err:.2%}", ""))
        if failed:
            ev.append(Evidence("ledger", "任务状态", "failed",
                               inp.run.get("error_msg") or ""))
        return [Finding(
            rule_id="X7_root_not_executable", layer="cross", severity="error",
            title="SQL 不可执行，本轮不构成有效压测",
            evidence=ev,
            root_cause="语句错误（语法 / 权限 / 对象不存在）或任务直接失败，"
                       "性能结论无意义，先修通语句",
            actions=[Action("sql", "修正报错语句后重新压测")],
        )]
    return []


def _x8(base_findings, comp):
    """健康结论必须带前提：样本充足 + 采集完整。"""
    if any(f.rule_id not in META_RULES for f in base_findings):
        return []
    if not (comp.get("sample_sufficient") and comp.get("plan_available")
            and comp.get("metrics_sufficient") and comp.get("duration_sufficient")):
        return []
    return [Finding(
        rule_id="X8_root_healthy", layer="cross", severity="info",
        title="本轮压测未发现性能问题",
        evidence=[Evidence("ledger", "总请求数", comp.get("total_requests"), "样本充足"),
                  Evidence("plan", "执行计划", "已采集且无坏味道", ""),
                  Evidence("engine", "引擎指标", f"{comp.get('metrics_points')} 个采样点", "")],
        root_cause="账本 / 引擎 / 计划三层均无命中",
        actions=[Action("benchmark", "想探边界可提高并发或放大数据量后再测")],
    )]


# ---- 总评与置信度 ----

def verdict(findings: list, comp: dict) -> dict:
    ids = {f.rule_id for f in findings}
    if "X7_root_not_executable" in ids:
        level, title = "not_usable", "SQL 不可执行，本轮不构成有效压测"
    else:
        # X8 本身就是"健康"结论，不计入 severity 聚合
        sev = {f.severity for f in findings if f.rule_id != "X8_root_healthy"}
        if "error" in sev:
            level, title = "severe", "存在严重问题，需优先修复"
        elif "warn" in sev:
            level, title = "needs_optimization", "存在可优化项"
        elif "info" in sev:
            level, title = "attention", "整体可用，有若干观察项"
        else:
            level, title = "healthy", "未发现性能问题"

    missing = []
    if not comp.get("sample_sufficient"):
        missing.append("样本不足")
    if not comp.get("plan_available"):
        missing.append("无执行计划证据")
    if not comp.get("metrics_sufficient"):
        missing.append("引擎指标缺失")
    if not comp.get("duration_sufficient"):
        missing.append("未跑满时长")
    confidence = "高" if not missing else ("中" if len(missing) == 1 else "低")

    summary = title
    if missing:
        summary += f"（置信度{confidence}：{'、'.join(missing)}）"
    if level == "healthy" and confidence != "高":
        summary = "未发现问题，但证据不足" + (f"（{('、'.join(missing))}）" if missing else "")
    return {"level": level, "title": title, "confidence": confidence, "summary": summary}
