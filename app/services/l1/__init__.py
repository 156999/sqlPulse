"""L1 降级报告：硬规则、零外部依赖，LLM 不可用时的默认诊断。

对外接口：
    build(inp, th)                 -> L1Report   # 纯函数，L1 的唯一入口
    render_markdown(run, l0, raw)  -> str        # 完整报告 Markdown
    build_view(raw, l0)            -> dict       # HTML 视图模型

规则四层（见 sqlpulse/docs/l1_report_design.md §5）：
    A 账本层（Locust 结果）→ B 引擎层（MySQL 运行时）→ C 计划层（EXPLAIN）
    → X 交叉归因层（≥2 来源同时命中才输出根因，吸收其子规则避免重复）。
"""
from app.services.l1.contract import L1Input, L1Report, META_RULES, SEV_ORDER, normalize_l1
from app.services.l1 import evidence, rules_cross, rules_ledger, rules_mysql, rules_plan, render
from app.services.l1.rules_plan import plan_indexes

__all__ = ["build", "render_markdown", "build_view", "L1Input", "L1Report", "normalize_l1"]


def build(inp: L1Input, th) -> L1Report:
    base = []
    base += rules_ledger.rules(inp, th)
    base += rules_mysql.rules(inp, th)
    base += rules_plan.rules(inp, th)

    comp = evidence.completeness(inp, th)
    cross = rules_cross.apply_cross(inp, th, base, comp)
    findings = rules_cross.dedup(base, cross)

    findings.sort(key=lambda f: (SEV_ORDER.get(f.severity, 9),
                                 0 if f.layer == "cross" else 1,
                                 0 if f.rule_id in META_RULES else 2,
                                 f.rule_id))

    return L1Report(
        verdict=rules_cross.verdict(findings, comp),
        findings=findings,
        per_sql=_per_sql_view(inp, findings),
        engine_view=inp.engine,
        completeness=comp,
        thresholds=th.snapshot(),
    )


def _per_sql_view(inp: L1Input, findings: list) -> list:
    """分语句明细 + 计划问题 + 命中规则，报告页与 Markdown 共用。"""
    idx = plan_indexes(inp.plan or {})
    code_label = {
        "table_scan": "全表扫描", "index_ignored": "索引未用", "full_index_scan": "全索引扫描",
        "filesort": "filesort", "temporary": "临时表", "join_buffer": "join无索引",
        "compile_error": "编译错误", "explain_error": "EXPLAIN报错",
        "skipped_not_explainable": "不可EXPLAIN", "not_probed": "未采集",
    }
    plan_by_sql: dict = {}
    for code, items in idx.items():
        for i in items:
            sid = i.get("sql_id")
            if not sid:
                continue
            label = code_label.get(code, code)
            plan_by_sql.setdefault(sid, []).append(label)

    hit_by_sql: dict = {}
    for f in findings:
        sid = (f.scope or {}).get("sql_id")
        if sid:
            hit_by_sql.setdefault(sid, []).append(f.rule_id)

    out = []
    for s in inp.ledger.get("per_sql") or []:
        sid = s.get("sql_id")
        plans = plan_by_sql.get(sid) or []
        # 保持频次信息：同一语句两处 table_scan → "全表扫描×2"
        counts: dict = {}
        for p in plans:
            counts[p] = counts.get(p, 0) + 1
        plan_str = ", ".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items())
        out.append({
            **s,
            "plan_findings": plan_str,
            "hit_rules": ", ".join(hit_by_sql.get(sid) or []),
        })
    return out


render_markdown = render.render_markdown
build_view = render.build_view
