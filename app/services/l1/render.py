"""L1Report → Markdown / HTML 视图模型。与 explain_view.py 同风格：
只做映射与排版，不做判断。两个消费方（report.md 下载、report.html 页面）
由同一份 L1Report 渲染，保证内容一致。
"""
from app.services.l1.contract import normalize_l1

LEVEL_CLS = {"error": "advice-error", "warn": "advice-warn",
             "info": "advice-info", "legacy": "advice-info"}
VERDICT_CLS = {"severe": "advice-error", "not_usable": "advice-error",
               "needs_optimization": "advice-warn", "attention": "advice-info",
               "healthy": "advice-ok", "legacy": "advice-info"}
ACTION_LABEL = {"sql": "SQL", "index": "索引", "config": "配置", "benchmark": "压测"}
LAYER_LABEL = {"ledger": "压测账本", "engine": "MySQL 引擎",
               "plan": "执行计划", "cross": "综合归因", "target": "环境",
               "legacy": "旧版"}


def _fmt(v, suffix=""):
    return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else "-"


# ---------- Markdown ----------

def render_markdown(run: dict, l0: dict, l1_raw) -> str:
    report = normalize_l1(l1_raw)
    lines = []
    lines += _sec_header(run, report)
    lines += _sec_verdict(run, l0, report)
    if report.get("legacy"):
        lines += _sec_legacy_findings(report)
        lines += _sec_legacy_tail(run, l0)
        return "\n".join(lines) + "\n"
    lines += _sec_findings(report)
    lines += _sec_per_sql(l0, report)
    lines += _sec_engine(report)
    lines += _sec_completeness(report)
    lines += _sec_appendix(run, l0, report)
    return "\n".join(lines) + "\n"


def _sec_header(run, report):
    v = report.get("verdict") or {}
    return [
        f"# 压测诊断报告 · {run.get('name') or run.get('id')}", "",
        f"> 报告等级：L1 · 本地规则报告（未配置 LLM）",
        f"> 生成时间：{run.get('ended_at') or '-'}   置信度：{v.get('confidence', '-')}", "",
    ]


def _sec_verdict(run, l0, report):
    v = report.get("verdict") or {}
    err = f"{l0['err_rate']:.2%}" if l0.get("err_rate") is not None else "-"
    out = ["## 1. 结论摘要", "", f"**{v.get('title', '-')}**", "",
           f"{v.get('summary', '')}", ""]
    out += ["| 指标 | 值 | 指标 | 值 |", "|---|---|---|---|",
            f"| QPS 均值 / 峰值 | {_fmt(l0.get('qps_avg'))} / {_fmt(l0.get('qps_peak'))} | "
            f"P95 / P99 | {_fmt(l0.get('p95_ms'), 'ms')} / {_fmt(l0.get('p99_ms'), 'ms')} |",
            f"| 错误率 | {err} | 总请求数 | {l0.get('total_requests') or 0} |"]
    return out


def _sec_findings(report):
    out = ["## 2. 问题清单", ""]
    findings = report.get("findings") or []
    if not findings:
        out += ["本轮无命中规则。", ""]
        return out
    for f in findings:
        out.append(f"### [{f['severity'].upper()}] {f['title']}")
        scope = f.get("scope") or {}
        parts = [str(x) for x in (scope.get("sql_id"), scope.get("table")) if x]
        if parts:
            out.append(f"- 定位：{' / '.join(parts)}")
        if f.get("evidence"):
            out.append("- 证据：")
            for e in f["evidence"]:
                label = LAYER_LABEL.get(e.get("source"), e.get("source"))
                detail = f"（{e['detail']}）" if e.get("detail") else ""
                out.append(f"  - [{label}] {e['label']}：{e['value']}{detail}")
        if f.get("root_cause"):
            out.append(f"- 根因：{f['root_cause']}")
        if f.get("actions"):
            out.append("- 修复建议：")
            for a in f["actions"]:
                out.append(f"  - [{ACTION_LABEL.get(a['layer'], a['layer'])}] {a['text']}")
        out.append("")
    return out


def _sec_per_sql(l0, report):
    per_map = {p.get("sql_id"): p for p in report.get("per_sql") or []}
    out = ["## 3. 分语句明细", "",
           "| 语句 | 内容 | 请求数 | 失败数 | 平均 | P95 | P99 | 最大 | QPS | 计划问题 | 命中规则 |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in l0.get("per_sql") or []:
        p = per_map.get(s.get("sql_id")) or {}
        out.append(
            f"| {s.get('sql_id')} | {s.get('sql', '')} | {s.get('requests')} | "
            f"{s.get('failures')} | {_fmt(s.get('avg_ms'), 'ms')} | {_fmt(s.get('p95_ms'), 'ms')} | "
            f"{_fmt(s.get('p99_ms'), 'ms')} | {_fmt(s.get('max_ms'), 'ms')} | {_fmt(s.get('qps'))} | "
            f"{p.get('plan_findings') or '-'} | {p.get('hit_rules') or '-'} |"
        )
    out.append("")
    return out


def _sec_engine(report):
    e = report.get("engine_view") or {}

    def g(k, nd=1):
        v = e.get(k)
        return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "-"

    hit = e.get("bufpool_hit")
    hit_str = f"{hit:.1%}" if isinstance(hit, (int, float)) else "-"
    return ["## 4. MySQL 侧观察", "",
            "| 指标 | 值 |", "|---|---|",
            f"| 采样点数 | {e.get('points') or 0} |",
            f"| 慢查询（增量） | {g('slow_total')} 条 |",
            f"| 行锁等待（增量） | {g('lock_waits_total')} 次（峰值 {g('lock_waits_rate_max')}/s） |",
            f"| 磁盘临时表（增量） | {g('tmp_disk_total')} 张 |",
            f"| 缓冲池命中率 | {hit_str} |",
            f"| Threads_running P95 | {g('threads_running_p95')} |",
            f"| 目标库 QPS（均值） | {g('mysql_qps_avg')} |",
            "",
            "> 口径：增量指标为压测期间差分合计；慢查询受目标库 long_query_time 影响，"
            "只能同库纵向对比；缓冲池命中率为累计口径。", ""]


def _sec_completeness(report):
    c = report.get("completeness") or {}
    plan_line = (f"已采集 {c.get('plan_probed') or 0} / {c.get('plan_statements') or 0} 条语句"
                 if c.get("plan_available") else (c.get("plan_reason") or "未采集"))
    return ["## 5. 采集完整性与局限", "",
            f"- 执行计划：{plan_line}",
            f"- 引擎指标：{c.get('metrics_points') or 0} 个采样点",
            f"- 样本量：{c.get('total_requests') or 0} 次请求"
            + ("" if c.get("sample_sufficient") else "（不足，分位结论仅供参考）"),
            f"- 时长比：{c.get('duration_ratio') or 0}"
            + ("" if c.get("duration_sufficient") else "（未跑满计划时长）"),
            "",
            "**已知局限**：",
            "1. EXPLAIN 的 rows 是优化器估算，不是真实扫描行数；表刚批量导入后统计信息可能过期。",
            "2. EXPLAIN 只解释读路径；写路径的锁竞争要看行锁等待增量。",
            "3. 采集发生在压测结束后，反映的是那一刻的统计信息。",
            "4. EXPLAIN 是按模板重放的另一次采样，代表典型值，不代表极端值。",
            "5. 慢查询增量受目标库 long_query_time 影响，只能同库纵向对比。",
            "6. 归因粒度是 task / sql_id，一个 task 内多条语句共享一次计时。",
            ""]


def _sec_appendix(run, l0, report):
    th = report.get("thresholds") or {}
    th_line = ", ".join(f"{k}={v}" for k, v in sorted(th.items())) if th else "-"
    return ["## 6. 附录", "",
            f"- 任务：{run.get('name')}（{run.get('id')}）· 状态 {run.get('status')}",
            f"- 并发 {run.get('concurrency')}（spawn {run.get('spawn_rate')}/s）"
            f"· 计划 {run.get('duration_sec')}s / 实际 {l0.get('actual_duration_sec') or '-'}s",
            f"- 生效阈值快照：{th_line}",
            "", "---", "*SQL Pulse · L1 本地规则报告 · L2 LLM 诊断段落（预留）*", ""]


# legacy（旧 list 形状的历史报告）

def _sec_legacy_findings(report):
    out = ["## 2. 规则建议（旧版引擎）", ""]
    for f in report.get("findings") or []:
        out.append(f"- **[{f['severity'].upper()}] {f['title']}**：{f.get('detail', '')}")
    out.append("")
    return out


def _sec_legacy_tail(run, l0):
    return ["---", "*本报告由旧版规则引擎生成，仅做兼容展示。*", ""]


# ---------- HTML 视图模型 ----------

def build_view(l1_raw, l0: dict = None) -> dict:
    report = normalize_l1(l1_raw)
    findings = report.get("findings") or []
    per_sql = report.get("per_sql") or (l0 or {}).get("per_sql") or []
    return {
        "available": True,
        "legacy": bool(report.get("legacy")),
        "verdict": report.get("verdict") or {},
        "verdict_cls": VERDICT_CLS.get((report.get("verdict") or {}).get("level"), "advice-info"),
        "findings": [_finding_view(f) for f in findings],
        "per_sql": per_sql,
        "engine_view": report.get("engine_view") or {},
        "completeness": report.get("completeness") or {},
        "thresholds": report.get("thresholds") or {},
        "error_count": sum(1 for f in findings if f.get("severity") == "error"),
        "warn_count": sum(1 for f in findings if f.get("severity") == "warn"),
    }


def _finding_view(f: dict) -> dict:
    scope = f.get("scope") or {}
    scope_text = " / ".join(str(v) for v in (scope.get("sql_id"), scope.get("table")) if v)
    return {
        **f,
        "cls": LEVEL_CLS.get(f.get("severity"), "advice-info"),
        "scope_text": scope_text,
        "evidence": [
            {**e, "source_label": LAYER_LABEL.get(e.get("source"), e.get("source"))}
            for e in (f.get("evidence") or [])
        ],
        "actions": [
            {**a, "layer_label": ACTION_LABEL.get(a.get("layer"), a.get("layer"))}
            for a in (f.get("actions") or [])
        ],
    }
