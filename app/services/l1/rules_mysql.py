"""B 层 · MySQL 引擎规则（10 条）：只看 collector 采集的运行时指标。

文件叫 rules_mysql 而不是 rules_engine，避免与旧的
app/services/rule_engine.py（顶层规则引擎，已降级为兼容壳）混淆。

采样点不足时 B10 独占输出（降级声明），其余 B 层规则全部跳过。
"""
from app.services.l1.contract import Action, Evidence, Finding


def rules(inp, th) -> list:
    eng = inp.engine
    if (eng.get("points") or 0) < th.min_points:
        return _b10_metrics_missing(eng, th)
    out = []
    out += _b1_slow_queries(eng, th)
    out += _b2_lock_waits(eng, th)
    out += _b3_lock_waits_heavy(eng, th)
    out += _b4_tmp_disk_tables(eng, th)
    out += _b5_bufpool_hit(eng, th)
    out += _b7_conn_near_limit(eng, inp.target, th)
    out += _b8_threads_exceed_concurrency(eng, inp.run, th)
    out += _b9_mysql_qps_amplification(eng, inp.ledger, th)
    return out


def _fmt(v, nd=0):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v)


def _b1_slow_queries(eng, th):
    # present=False 是"没采到"，与"真实为 0"（健康）严格区分
    if not eng.get("slow_present"):
        return []
    v = eng.get("slow_total") or 0
    if v <= th.slow_total:
        return []
    return [Finding(
        rule_id="B1_slow_queries", layer="engine", severity="warn",
        title=f"目标库记录了 {_fmt(v)} 条慢查询",
        evidence=[Evidence("engine", "Slow_queries 增量合计", _fmt(v),
                           "受目标库 long_query_time 影响，只能同库纵向对比")],
        root_cause="目标库自身认定存在慢查询；与压测观测一致时可信度高",
        actions=[Action("config", "确认目标库 long_query_time 取值再解读该数值"),
                 Action("sql", "结合分语句明细定位最慢语句")],
    )]


def _b2_lock_waits(eng, th):
    if not eng.get("lock_waits_present"):
        return []
    v = eng.get("lock_waits_total") or 0
    if v <= th.lock_total:
        return []
    return [Finding(
        rule_id="B2_lock_waits", layer="engine", severity="warn",
        title=f"行锁等待 {_fmt(v)} 次",
        evidence=[Evidence("engine", "Innodb_row_lock_waits 增量合计", _fmt(v), "")],
        root_cause="存在行锁竞争：热点行、长事务或大范围扫描持锁",
        actions=[Action("sql", "检查写语句是否可缩短事务 / 拆批"),
                 Action("config", "确认隔离级别与锁超时配置")],
    )]


def _b3_lock_waits_heavy(eng, th):
    v = eng.get("lock_waits_rate_max")
    if v is None or v <= th.lock_rate:
        return []
    return [Finding(
        rule_id="B3_lock_waits_heavy", layer="engine", severity="error",
        title=f"锁等待峰值达 {_fmt(v, 1)} 次/秒",
        evidence=[Evidence("engine", "单秒锁等待峰值", _fmt(v, 1),
                           f"阈值 {th.lock_rate}/s")],
        root_cause="严重的行锁竞争，尾延迟大概率由此主导",
        actions=[Action("sql", "定位热点行：缩小事务、避免热点更新"),
                 Action("benchmark", "降低并发对比验证")],
    )]


def _b4_tmp_disk_tables(eng, th):
    if not eng.get("tmp_disk_present"):
        return []
    v = eng.get("tmp_disk_total") or 0
    if v <= th.tmp_total:
        return []
    return [Finding(
        rule_id="B4_tmp_disk_tables", layer="engine", severity="warn",
        title=f"产生 {_fmt(v)} 张磁盘临时表",
        evidence=[Evidence("engine", "Created_tmp_disk_tables 增量合计", _fmt(v), "")],
        root_cause="GROUP BY / ORDER BY / DISTINCT 无法在内存或索引内完成，落盘排序",
        actions=[Action("index", "为 ORDER BY / GROUP BY 列建索引"),
                 Action("sql", "减少结果集与 DISTINCT 的使用")],
    )]


def _b5_bufpool_hit(eng, th):
    """B5/B6 同一指标两档，只输出档位最高的一条。"""
    v = eng.get("bufpool_hit")
    if v is None or v >= th.bufpool_hit:
        return []
    sev = "warn" if v < th.bufpool_hit_bad else "info"
    rid = "B6_bufpool_hit_bad" if sev == "warn" else "B5_bufpool_hit_low"
    return [Finding(
        rule_id=rid, layer="engine", severity=sev,
        title=f"缓冲池命中率偏低（{v:.1%}）",
        evidence=[Evidence("engine", "Innodb_buffer_pool 命中率", f"{v:.1%}",
                           "累计口径，非差分")],
        root_cause="缓冲池不足以容纳工作集，或存在大范围扫描带来大量冷读",
        actions=[Action("config", "评估 innodb_buffer_pool_size"),
                 Action("index", "收窄扫描范围，减少冷读")],
    )]


def _b7_conn_near_limit(eng, target, th):
    tr = eng.get("threads_running_p95")
    max_conn = (target or {}).get("max_connections")
    if tr is None or not max_conn:
        return []
    ratio = tr / max_conn
    if ratio <= th.conn_ratio:
        return []
    return [Finding(
        rule_id="B7_conn_near_limit", layer="engine", severity="warn",
        title=f"Threads_running P95 达 max_connections 的 {ratio:.0%}",
        evidence=[Evidence("engine", "Threads_running P95", _fmt(tr),
                           f"max_connections = {max_conn}，比值 {ratio:.2f}")],
        root_cause="并发执行已接近连接上限：可能是慢查询堆积占满连接",
        actions=[Action("config", "检查连接池配置与 max_connections"),
                 Action("sql", "处理慢语句以释放连接")],
    )]


def _b8_threads_exceed_concurrency(eng, run, th):
    tr = eng.get("threads_running_p95")
    conc = (run or {}).get("concurrency")
    if tr is None or not conc:
        return []
    if tr <= conc * th.tr_factor:
        return []
    return [Finding(
        rule_id="B8_threads_exceed_concurrency", layer="engine", severity="info",
        title=f"目标库活跃线程数（P95 {tr:.0f}）超过压测并发（{conc}）",
        evidence=[Evidence("engine", "Threads_running P95", _fmt(tr), f"压测并发 {conc}")],
        root_cause="目标库上还有压测之外的流量，或单请求触发多条语句；归因时需剔除这部分影响",
        actions=[Action("benchmark", "确认目标库是否为压测独占环境")],
    )]


def _b9_mysql_qps_amplification(eng, led, th):
    mq = eng.get("mysql_qps_avg")
    aq = led.get("qps_avg")
    if mq is None or aq is None or aq <= 0:
        return []
    amp = mq / aq
    if amp <= th.qps_amp:
        return []
    return [Finding(
        rule_id="B9_mysql_qps_amplification", layer="engine", severity="info",
        title=f"目标库 QPS（{_fmt(mq, 1)}）是应用侧（{_fmt(aq, 1)}）的 {amp:.1f} 倍",
        evidence=[Evidence("engine", "目标库 Questions/s 均值", _fmt(mq, 1),
                           f"应用侧 QPS 均值 {_fmt(aq, 1)}，阈值 {th.qps_amp} 倍")],
        root_cause="单请求触发多条语句，或目标库存在压测外流量",
        actions=[Action("benchmark", "确认倍数是否符合业务预期（一个请求多条 SQL）")],
    )]


def _b10_metrics_missing(eng, th):
    return [Finding(
        rule_id="B10_metrics_missing", layer="engine", severity="info",
        title="引擎指标采样点不足，B 层规则不可用",
        evidence=[Evidence("engine", "采样点数", eng.get("points") or 0,
                           f"阈值 {th.min_points}")],
        root_cause="压测时间过短或目标库连不上，MySQL 运行时指标缺失，本报告不含引擎层结论",
        actions=[Action("benchmark", "确认目标库可连通后重跑")],
    )]
