"""A 层 · 压测账本规则（13 条）：只看 Locust 结果，不看引擎与计划。

每条规则一个小函数，返回 list[Finding]（0 或 1 条）。不用 DSL / 元组表，
便于逐条单测与断点调试。全部 None 安全：指标缺失时静默跳过。
"""
from app.services.l1.contract import Action, Evidence, Finding


def rules(inp, th) -> list:
    led = inp.ledger
    out = []
    out += _a1_p99_high(led, th)
    out += _a2_p99_severe(led, th)
    out += _a3_p95_high(led, th)
    out += _a4_long_tail(led, th)
    out += _a5_slow_statement(led, th)
    out += _a6_slow_statement_severe(led, th)
    out += _a7_err_rate(led, th)
    out += _a8_failures_concentrated(led, th)
    out += _a9_throughput_below_expectation(inp, th)
    out += _a10_throughput_plateau(inp, th)
    out += _a11_stability_jitter(inp, th)
    out += _a12_duration_short(led, th)
    out += _a13_low_sample(led, th)
    return out


def _fmt(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else str(v)


# ---- 汇总延迟 ----

def _a1_p99_high(led, th):
    v = led.get("p99_ms")
    if v is None or v <= th.p99_ms:
        return []
    return [Finding(
        rule_id="A1_p99_high", layer="ledger", severity="warn",
        title=f"P99 延迟超过告警线（{_fmt(v)}ms > {th.p99_ms:.0f}ms）",
        evidence=[Evidence("ledger", "汇总 P99", _fmt(v) + "ms", f"告警线 {th.p99_ms:.0f}ms")],
        root_cause="尾部延迟偏高，需定位到具体语句与执行计划",
        actions=[Action("benchmark", "查看分语句明细，定位 P99 最高的语句")],
    )]


def _a2_p99_severe(led, th):
    v = led.get("p99_ms")
    if v is None or v <= th.p99_severe_ms:
        return []
    return [Finding(
        rule_id="A2_p99_severe", layer="ledger", severity="error",
        title=f"P99 延迟严重超标（{_fmt(v)}ms > {th.p99_severe_ms:.0f}ms）",
        evidence=[Evidence("ledger", "汇总 P99", _fmt(v) + "ms", f"严重线 {th.p99_severe_ms:.0f}ms")],
        root_cause="尾部延迟由少数慢语句主导，需定位到具体语句与执行计划",
        actions=[Action("sql", "对最慢语句做 EXPLAIN，确认是否走索引"),
                 Action("benchmark", "修复后复跑同一压测对比")],
    )]


def _a3_p95_high(led, th):
    v = led.get("p95_ms")
    if v is None or v <= th.p95_ms:
        return []
    return [Finding(
        rule_id="A3_p95_high", layer="ledger", severity="warn",
        title=f"P95 延迟超过告警线（{_fmt(v)}ms > {th.p95_ms:.0f}ms）",
        evidence=[Evidence("ledger", "汇总 P95", _fmt(v) + "ms", f"告警线 {th.p95_ms:.0f}ms")],
        root_cause="不只是尾部：第 95 分位也已超标，慢是普遍现象",
        actions=[Action("benchmark", "对照分语句明细，确认是单语句拖累还是整体偏慢")],
    )]


def _a4_long_tail(led, th):
    p99, p50 = led.get("p99_ms"), led.get("p50_ms")
    if p99 is None or p50 is None or p50 <= 0:
        return []
    ratio = p99 / p50
    if ratio <= th.tail_ratio or p99 <= th.p99_ms:
        return []
    return [Finding(
        rule_id="A4_long_tail", layer="ledger", severity="warn",
        title=f"长尾效应明显（P99/P50 = {ratio:.1f} 倍）",
        evidence=[Evidence("ledger", "P99 / P50", f"{_fmt(p99)}ms / {_fmt(p50)}ms",
                           f"比值 {ratio:.1f} > {th.tail_ratio:.0f}")],
        root_cause="中位数尚可但尾部失控：偶发锁等待、刷盘或执行计划抖动",
        actions=[Action("benchmark", "结合行锁等待与慢查询增量判断是否偶发锁竞争")],
    )]


# ---- 分语句 ----

def _a5_slow_statement(led, th):
    per = led.get("per_sql") or []
    slow = [s for s in per if (s.get("avg_ms") or 0) > th.sql_avg_ms]
    if not slow:
        return []
    slow.sort(key=lambda s: -(s.get("avg_ms") or 0))
    top = ", ".join(f"{s['sql_id']}({(s.get('avg_ms') or 0):.0f}ms)" for s in slow[:3])
    return [Finding(
        rule_id="A5_slow_statement", layer="ledger", severity="warn",
        title=f"存在慢语句：{top}",
        scope={"sql_id": slow[0]["sql_id"]},
        evidence=[Evidence("ledger", "最慢语句平均延迟", f"{(slow[0].get('avg_ms') or 0):.0f}ms",
                           f"告警线 {th.sql_avg_ms:.0f}ms，共 {len(slow)} 条超线")],
        root_cause="单条语句平均耗时明显偏高",
        actions=[Action("sql", "逐条检查慢语句的执行计划"),
                 Action("index", "确认谓词列是否有可用索引")],
    )]


def _a6_slow_statement_severe(led, th):
    per = led.get("per_sql") or []
    severe = [s for s in per if (s.get("p99_ms") or 0) > th.sql_p99_ms]
    if not severe:
        return []
    severe.sort(key=lambda s: -(s.get("p99_ms") or 0))
    s = severe[0]
    return [Finding(
        rule_id="A6_slow_statement_severe", layer="ledger", severity="error",
        title=f"{s['sql_id']} 的 P99 达 {s.get('p99_ms'):.0f}ms，严重超标",
        scope={"sql_id": s["sql_id"]},
        evidence=[Evidence("ledger", "该语句 P99", f"{s.get('p99_ms'):.0f}ms",
                           f"严重线 {th.sql_p99_ms:.0f}ms"),
                  Evidence("ledger", "该语句 SQL", s.get("sql") or "", "")],
        root_cause="该语句的尾延迟已达严重级别，优先修复",
        actions=[Action("sql", "优先处理该语句：EXPLAIN 确认执行计划"),
                 Action("benchmark", "修复后复跑，对比该语句 P99")],
    )]


# ---- 错误 ----

def _a7_err_rate(led, th):
    v = led.get("err_rate")
    if v is None:
        return []
    if v > th.err_rate:
        sev, rid = "error", "A7_err_rate"
    elif v > th.err_rate_warn:
        sev, rid = "warn", "A7_err_rate"
    else:
        return []
    return [Finding(
        rule_id=rid, layer="ledger", severity=sev,
        title=f"错误率 {v:.2%}" + ("，超过 1%" if sev == "error" else "，值得关注"),
        evidence=[Evidence("ledger", "错误率", f"{v:.2%}",
                           f"总失败 {led.get('total_failures') or 0} / "
                           f"{led.get('total_requests') or 0} 请求")],
        root_cause="失败可能来自语法错误、锁超时或唯一键冲突，需看失败是否集中在个别语句",
        actions=[Action("sql", "检查失败集中语句的报错信息"),
                 Action("benchmark", "修正后复跑")],
    )]


def _a8_failures_concentrated(led, th):
    per = led.get("per_sql") or []
    total_fail = sum(s.get("failures") or 0 for s in per)
    if total_fail <= 0:
        return []
    worst = max(per, key=lambda s: s.get("failures") or 0)
    share = (worst.get("failures") or 0) / total_fail
    if share < th.fail_share:
        return []
    return [Finding(
        rule_id="A8_failures_concentrated", layer="ledger", severity="warn",
        title=f"失败集中在 {worst['sql_id']}（占全部失败的 {share:.0%}）",
        scope={"sql_id": worst["sql_id"]},
        evidence=[Evidence("ledger", "该语句失败数", worst.get("failures") or 0,
                           f"占全部失败 {share:.0%}"),
                  Evidence("ledger", "该语句 SQL", worst.get("sql") or "", "")],
        root_cause="失败不是全局性的，大概率是该语句自身的语法/表名/权限/约束问题",
        actions=[Action("sql", "单独执行该语句，确认报错信息"),
                 Action("benchmark", "修正后复跑同一压测")],
    )]


# ---- 吞吐 ----

def _a9_throughput_below_expectation(inp, th):
    led = inp.ledger
    qps_peak, conc = led.get("qps_peak"), inp.run.get("concurrency")
    if qps_peak is None or not conc:
        return []
    if qps_peak >= conc * th.qps_factor:
        return []
    return [Finding(
        rule_id="A9_throughput_below_expectation", layer="ledger", severity="info",
        title=f"吞吐量低于并发预期（QPS 峰值 {_fmt(qps_peak)} < 并发 {conc} × {th.qps_factor}）",
        evidence=[Evidence("ledger", "QPS 峰值", _fmt(qps_peak), f"并发 {conc}，因子 {th.qps_factor}")],
        root_cause="并发线程没有跑满：存在等待（锁 / IO / 连接），需结合引擎层指标区分",
        actions=[Action("benchmark", "对照行锁等待与慢查询增量，区分锁竞争与 IO 瓶颈")],
    )]


def _total_qps_series(series: dict) -> list:
    """{sql_id: [...]} → 按 ts 聚合的总 RPS 时序（升序）。"""
    by_ts: dict = {}
    for points in (series or {}).values():
        for p in points:
            rps = p.get("rps")
            if rps is not None:
                by_ts[p.get("ts", 0)] = by_ts.get(p.get("ts", 0), 0.0) + rps
    return [by_ts[ts] for ts in sorted(by_ts)]


def _slope(ys: list) -> float:
    n = len(ys)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _a10_throughput_plateau(inp, th):
    ys = _total_qps_series(inp.series)
    if len(ys) < 6:          # 太短的窗口拟合斜率没有意义
        return []
    mid = len(ys) // 2
    first, second = ys[:mid], ys[mid:]
    mean = sum(second) / len(second)
    if mean <= 0:
        return []
    # "并发爬坡后走平"才算平台期：前半程要有爬坡证据（归一化斜率过阈值），
    # 全程平稳的序列无法区分"已达上限"与"负载本就恒定"，不判
    first_mean = sum(first) / len(first)
    if first_mean <= 0 or abs(_slope(first)) / first_mean < th.plateau_slope:
        return []
    norm = abs(_slope(second)) / mean
    if norm >= th.plateau_slope:
        return []
    return [Finding(
        rule_id="A10_throughput_plateau", layer="ledger", severity="info",
        title=f"吞吐已进入平台期（后半程 QPS 斜率 ≈ {norm:.3f}）",
        evidence=[Evidence("ledger", "后半程归一化斜率", f"{norm:.3f}",
                           f"阈值 {th.plateau_slope}，均值 QPS {_fmt(mean)}")],
        root_cause="并发爬坡完成后 QPS 不再增长：系统已达当前配置下的吞吐上限",
        actions=[Action("benchmark", "若要继续提升吞吐，先解决其它命中的性能问题再复跑")],
    )]


def _a11_stability_jitter(inp, th):
    vals = [p.get("p99_ms") for points in (inp.series or {}).values()
            for p in points if p.get("p99_ms") is not None]
    if len(vals) < 5:
        return []
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return []
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    cv = (var ** 0.5) / mean
    if cv <= th.cv:
        return []
    return [Finding(
        rule_id="A11_stability_jitter", layer="ledger", severity="info",
        title=f"延迟抖动明显（P99 变异系数 {cv:.2f}）",
        evidence=[Evidence("ledger", "P99 时序变异系数", f"{cv:.2f}",
                           f"阈值 {th.cv}，样本 {len(vals)} 点")],
        root_cause="耗时忽高忽低：偶发锁等待、缓冲池抖动或统计信息变化",
        actions=[Action("benchmark", "结合引擎指标时间轴定位抖动发生的时间窗")],
    )]


# ---- 元规则（只影响置信度，不算性能问题）----

def _a12_duration_short(led, th):
    duration = led.get("duration_sec") or 0
    actual = led.get("actual_duration_sec") or 0
    if not duration or not actual:
        return []
    ratio = actual / duration
    if ratio >= th.duration_ratio:
        return []
    return [Finding(
        rule_id="A12_duration_short", layer="ledger", severity="info",
        title=f"实际时长只有计划的 {ratio:.0%}",
        evidence=[Evidence("ledger", "实际 / 计划时长", f"{actual}s / {duration}s",
                           f"阈值 {th.duration_ratio:.0%}")],
        root_cause="压测未跑满计划时长（可能提前出错退出），结论置信度下降",
        actions=[Action("benchmark", "确认退出原因后重跑完整时长")],
    )]


def _a13_low_sample(led, th):
    n = led.get("total_requests") or 0
    if n >= th.min_requests:
        return []
    return [Finding(
        rule_id="A13_low_sample", layer="ledger", severity="info",
        title=f"样本不足（{n} 次请求 < {th.min_requests}）",
        evidence=[Evidence("ledger", "总请求数", n, f"阈值 {th.min_requests}")],
        root_cause="样本不足时分位数不可信，本报告的分位结论仅供参考",
        actions=[Action("benchmark", "提高并发或延长时长后复跑")],
    )]
