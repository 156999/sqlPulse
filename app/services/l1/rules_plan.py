"""C 层 · EXPLAIN 计划规则（10 条）：只看 explain_probe 产物。

证据按 sql_id 聚合 —— 这是 X 层交叉归因的基础：
消费方看到"sql_2 慢"时按 sql_id 取 statements[] 展开，不能假设一对一。
"""
from app.services.l1.contract import Action, Evidence, Finding

# 计划质量类 code（参与判断"计划是否健康"；bookkeeping 类不算）
QUALITY_CODES = ("table_scan", "index_ignored", "full_index_scan",
                 "filesort", "temporary", "join_buffer",
                 "compile_error", "explain_error")

# code → (rule_id, severity, 标题模板, 根因, 修复建议)
_SIMPLE = {
    "table_scan": (
        "C1_plan_table_scan", "warn", "{n} 处全表扫描",
        "谓词未命中任何索引，整表逐行过滤；数据量增长后延迟线性恶化",
        [("index", "按 WHERE / JOIN 谓词列评估索引"),
         ("sql", "检查谓词是否对列做了函数或类型转换")],
    ),
    "index_ignored": (
        "C2_plan_index_ignored", "warn", "{n} 处存在可用索引但未被使用",
        "possible_keys 有值但 key=NULL：类型不匹配、函数包裹列、或前置通配 LIKE '%x'",
        [("sql", "检查谓词是否对索引列做函数/隐式类型转换"),
         ("index", "按 WHERE / JOIN 列评估更合适的索引"),
         ("benchmark", "改后复跑，对比 P99")],
    ),
    "full_index_scan": (
        "C3_plan_full_index_scan", "info", "{n} 处全索引扫描",
        "扫整棵索引树（type=index）：通常意味着可用索引不符合查询形态",
        [("index", "评估与查询形态匹配的索引（覆盖排序/过滤列）")],
    ),
    "filesort": (
        "C4_plan_filesort", "warn", "{n} 处需要额外排序（filesort）",
        "ORDER BY 未走索引，结果集需要在内存/磁盘排序",
        [("index", "为 ORDER BY 列建索引（或与 WHERE 列组成复合索引）"),
         ("sql", "确认排序是否有业务必要")],
    ),
    "temporary": (
        "C5_plan_temporary", "warn", "{n} 处需要临时表",
        "GROUP BY / DISTINCT / 某些 JOIN 无法用索引完成，需要物化中间结果",
        [("index", "为 GROUP BY / DISTINCT 列建索引"),
         ("sql", "减少 DISTINCT 与中间结果集")],
    ),
    "join_buffer": (
        "C6_plan_join_buffer", "warn", "{n} 处 JOIN 未走索引（join buffer）",
        "JOIN 列无可用索引，被驱动表走块嵌套循环",
        [("index", "为被驱动表的 JOIN 列建索引")],
    ),
}


def rules(inp, th) -> list:
    plan = inp.plan
    if not plan:
        return _c10_plan_unavailable()
    summary = plan.get("summary") or {}
    idx = plan_indexes(plan)
    out = []
    for code, (rule_id, sev, title, cause, acts) in _SIMPLE.items():
        items = idx.get(code) or []
        if not items:
            continue
        sql_ids = sorted({i["sql_id"] for i in items if i["sql_id"]})
        tables = sorted({i["table"] for i in items if i["table"]})
        ev = [
            Evidence("plan", "涉及语句", ", ".join(sql_ids) or "-", ""),
            Evidence("plan", "涉及表", ", ".join(tables) or "-", ""),
        ] + [Evidence("plan", i["table"] or "-", i["detail"], "") for i in items[:3]]
        out.append(Finding(
            rule_id=rule_id, layer="plan", severity=sev,
            title=title.format(n=len(items)),
            scope={"sql_id": sql_ids[0] if sql_ids else None,
                   "table": tables[0] if tables else None},
            evidence=ev,
            root_cause=cause,
            actions=[Action(layer, text) for layer, text in acts],
        ))
    out += _c7_large_scan(summary, th)
    out += _c8_worst_error(summary)
    out += _c9_explain_error(idx)
    return out


def plan_indexes(plan) -> dict:
    """{code: [{sql_id, table, detail, rows}]}，C 层与 X 层共用。"""
    out: dict = {}
    for t in (plan or {}).get("tasks") or []:
        sql_id = t.get("sql_id")
        for st in t.get("statements") or []:
            for f in st.get("findings") or []:
                out.setdefault(f.get("code"), []).append({
                    "sql_id": sql_id,
                    "table": f.get("table"),
                    "detail": f.get("detail") or "",
                    "rows": st.get("max_rows"),
                })
    return out


def plan_healthy(plan) -> bool:
    """C 层读路径健康 = 无任何计划质量类 finding。X 层据此判定
    锁竞争不在读路径（X5 写路径归因）。plan 缺失时无法确认健康。"""
    if not plan:
        return False
    for t in plan.get("tasks") or []:
        for st in t.get("statements") or []:
            for f in st.get("findings") or []:
                if f.get("code") in QUALITY_CODES:
                    return False
    return True


def _fmt_rows(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def _c7_large_scan(summary, th):
    v = summary.get("max_rows")
    ref = summary.get("max_rows_ref") or {}
    if v is None or v <= th.max_rows:
        return []
    where = f"{ref.get('sql_id')} 第 {(ref.get('index') or 0) + 1} 条" if ref.get("sql_id") else "未知位置"
    return [Finding(
        rule_id="C7_plan_large_scan", layer="plan", severity="warn",
        title=f"预估最大扫描行数 {_fmt_rows(v)}",
        scope={"sql_id": ref.get("sql_id")},
        evidence=[Evidence("plan", "max_rows", _fmt_rows(v),
                           f"位于 {where}；阈值 {th.max_rows:,}，rows 为优化器估算")],
        root_cause="存在量级很大的扫描（估算值）：即使 type 不是 ALL，扫描量本身也已构成瓶颈",
        actions=[Action("index", "用更精确的索引收窄扫描范围"),
                 Action("sql", "确认查询是否缺少过滤条件")],
    )]


def _c8_worst_error(summary):
    if summary.get("worst_level") != "error":
        return []
    return [Finding(
        rule_id="C8_plan_worst_error", layer="plan", severity="error",
        title="执行计划层存在错误级问题",
        evidence=[Evidence("plan", "worst_level", "error",
                           "EXPLAIN 报错或编译失败，见计划明细")],
        root_cause="语句本身可能有问题（语法/权限/对象不存在），不是纯性能问题",
        actions=[Action("sql", "单独执行报错语句，先修通再谈性能")],
    )]


def _c9_explain_error(idx):
    items = idx.get("explain_error") or []
    if not items:
        return []
    return [Finding(
        rule_id="C9_plan_explain_error", layer="plan", severity="warn",
        title=f"{len(items)} 条语句 EXPLAIN 报错",
        evidence=[Evidence("plan", "报错语句", ", ".join(sorted(
            {i["sql_id"] for i in items if i["sql_id"]})) or "-", ""),
            Evidence("plan", "报错详情", items[0]["detail"], "")],
        root_cause="语句可能引用了不存在的对象或权限不足",
        actions=[Action("sql", "核对报错语句的表名 / 列名 / 权限")],
    )]


def _c10_plan_unavailable():
    from app.services.l1.evidence import plan_reason
    return [Finding(
        rule_id="C10_plan_unavailable", layer="plan", severity="info",
        title="无执行计划证据，C 层规则不可用",
        evidence=[Evidence("plan", "产物缺失原因", plan_reason(None), "")],
        root_cause="缺少执行计划证据时，无法归因到索引/计划层面，仅有账本与引擎侧结论",
        actions=[Action("benchmark", "重跑一次完整压测以采集执行计划")],
    )]
