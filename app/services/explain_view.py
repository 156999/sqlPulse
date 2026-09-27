"""EXPLAIN 采集产物的**视图模型**（展示层适配）。

职责单一：把 `data/explain/{run_id}.json` 这份机器产物翻译成模板可以直接渲染的结构。

两条设计边界（改动时别破）：

1. **纯函数**。不读文件、不连数据库、不引入 LLM。读产物由调用方用
   `explain_probe.load_artifact()` 完成。
2. **只做翻译，不做判断**。`findings` 的等级（info/warn/error）由 `classify_plan()`
   决定，本模块**只负责映射**成 CSS 类与中文标签。
   需要"为什么慢 / 该怎么改"那类推理属于 L2 LLM 诊断段落（`app/ai/`，仍是预留状态），不放这里。

为什么单独一层：模板里写等级映射会无法单测，而这个映射恰好是最容易出错的地方
（`type` 的大小写、`Using index condition` 与 `Using index` 的前缀关系 —— 见
`tests/unit/test_explain_view.py`）。
"""
from typing import Any, Optional

# findings.level → 项目既有的提示块样式（app.css 里已有 .advice-info/-warn/-error）
LEVEL_CLS = {"info": "advice-info", "warn": "advice-warn", "error": "advice-error"}

# findings.code → 中文短标签
CODE_LABEL = {
    "table_scan": "全表扫描",
    "index_ignored": "索引失效",
    "full_index_scan": "全索引扫描",
    "filesort": "额外排序",
    "temporary": "临时表",
    "join_buffer": "Join 缓冲",
    "skipped_not_explainable": "不可 EXPLAIN",
    "not_probed": "未采集",
    "explain_error": "EXPLAIN 失败",
    "compile_error": "编译失败",
}

# 查询类型 → (等级, 中文说明)。键一律小写比较：产物里 MySQL 原样输出
# `ALL` 是大写、`const`/`ref`/`index` 是小写，混用会漏判（`classify_plan` 踩过一次）。
_ACCESS = {
    "system": ("good", "常量表"),
    "const": ("good", "常量定位"),
    "eq_ref": ("good", "唯一索引等值"),
    "ref": ("ok", "索引查找"),
    "ref_or_null": ("ok", "索引查找"),
    "range": ("ok", "索引范围"),
    "index_merge": ("ok", "索引合并"),
    "fulltext": ("ok", "全文索引"),
    "index_subquery": ("ok", "索引子查询"),
    "unique_subquery": ("ok", "唯一索引子查询"),
    "index": ("warn", "全索引扫描"),
    "all": ("bad", "全表扫描"),
}
_ACCESS_DEFAULT = ("unknown", "")

# 语句的采集状态 → (中文标签, 徽章 CSS 后缀)
_STATUS = {
    "probed": ("已采集", "ex-badge-ok"),
    "skipped_not_explainable": ("不可 EXPLAIN", "ex-badge-muted"),
    "not_probed": ("超出额度未采集", "ex-badge-muted"),
    "compile_error": ("占位符取值失败", "ex-badge-warn"),
    "explain_error": ("EXPLAIN 失败", "ex-badge-warn"),
    "unknown": ("未采集", "ex-badge-muted"),
}

# 纯事实记录类，不计入"值得关注"的处数（与 worst_level 口径保持一致）
_BOOKKEEPING = ("skipped_not_explainable", "not_probed")


def access_of(type_value: Any) -> tuple:
    """EXPLAIN 的 type 列 → (等级, 中文说明)。"""
    key = (type_value or "").strip().lower()
    return _ACCESS.get(key, _ACCESS_DEFAULT)


def extra_tags(extra: Any) -> list:
    """Extra 列 → 标签列表 [{"cls","text"}]。

    顺序敏感：`Using index condition` 是 ICP（索引条件下推，中性偏好），
    而 `Using index` 是覆盖索引（好）。前者**包含**后者的子串，
    所以必须先判长的那个，否则覆盖索引的绿标签会错贴到 ICP 上。
    """
    if not extra:
        return []
    low = str(extra).lower()
    tags = []
    for token, level, text in (
        ("using filesort", "warn", "额外排序"),
        ("using temporary", "warn", "临时表"),
        ("using join buffer", "warn", "join 缓冲"),
    ):
        if token in low:
            tags.append({"cls": f"ex-{level}", "text": text})
    if "using index condition" in low:
        tags.append({"cls": "ex-ok", "text": "索引下推"})
    elif "using index" in low:
        tags.append({"cls": "ex-good", "text": "覆盖索引"})
    if "no matching row" in low:
        tags.append({"cls": "ex-ok", "text": "无匹配行"})
    elif "using where" in low:
        tags.append({"cls": "ex-ok", "text": "where 过滤"})
    return tags


def _status_of(entry: dict) -> str:
    """单条语句的采集状态。`ok=True` 才算真正采到计划。"""
    if entry.get("ok"):
        return "probed"
    codes = [f.get("code") for f in entry.get("findings") or []]
    for c in ("skipped_not_explainable", "not_probed", "compile_error", "explain_error"):
        if c in codes:
            return c
    return "unknown"


def _num(v: Any) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) else None


def _fmt_rows(v: Any) -> str:
    n = _num(v)
    return f"{n:,}" if n is not None else "—"


def _fmt_target(target: dict) -> str:
    if not target:
        return "—"
    host = target.get("host") or "?"
    port = target.get("port")
    db = target.get("database") or "?"
    return f"{host}:{port}/{db}" if port else f"{host}/{db}"


def _finding_view(f: dict) -> dict:
    level = f.get("level") or "info"
    code = f.get("code") or "?"
    return {
        "level": level,
        "cls": LEVEL_CLS.get(level, "advice-info"),
        "code": code,
        "label": CODE_LABEL.get(code, code),
        "table": f.get("table"),
        "detail": f.get("detail") or "",
    }


def _plan_view(plan: Any) -> list:
    rows_out = []
    for r in plan or []:
        level, text = access_of(r.get("type"))
        raw_type = r.get("type")
        rows_out.append({
            "table": r.get("table") or "?",
            "access": raw_type or "—",
            "access_cls": f"ex-{level}",
            "access_text": text,
            "key": r.get("key") or "—",
            "key_is_null": not r.get("key"),
            "possible_keys": r.get("possible_keys") or "—",
            "rows": _fmt_rows(r.get("rows")),
            "extra": r.get("Extra") or "",
            "extra_tags": extra_tags(r.get("Extra")),
        })
    return rows_out


def _params_view(params: Any) -> list:
    """绑定值 → 展示用短串（值本身已经在 `filled` 的 SQL 里了，这里只做速览）。"""
    out = []
    for p in params or []:
        if not isinstance(p, dict):
            out.append({"type": "?", "value": str(p)})
            continue
        value = p.get("value")
        text = "NULL" if value is None else str(value)
        if len(text) > 60:
            text = text[:60] + "…"
        out.append({"type": p.get("type") or "?", "value": text})
    return out


def _statement_view(entry: dict, no: int) -> dict:
    status = _status_of(entry)
    label, badge = _STATUS.get(status, _STATUS["unknown"])
    original = entry.get("original") or ""
    filled = entry.get("filled")
    return {
        "no": no,
        "status": status,
        "status_label": label,
        "status_badge": badge,
        "original": original,
        # 只有绑定结果与原文不同才展示，避免同样的 SQL 显示两遍
        "filled": filled if (filled and filled != original) else None,
        "params": _params_view(entry.get("parameters")),
        "findings": [_finding_view(f) for f in entry.get("findings") or []],
        "plan": _plan_view(entry.get("plan")),
        "max_rows": _fmt_rows(entry.get("max_rows")) if entry.get("max_rows") is not None else None,
    }


def _short_sql(sql: str, limit: int = 24) -> str:
    """折成一行并截断，用于折叠区的一行摘要。"""
    one = " ".join((sql or "").split())
    if not one:
        return "—"
    return one if len(one) <= limit else one[:limit] + "…"


def _skipped_view(st: dict) -> dict:
    """被跳过的语句（事务控制 / SET、超额度）只留"是哪条、为什么没采"。

    它们**不单独占一张卡片**：徽章已经写了"不可 EXPLAIN"，再配一条只说同一件事的
    提示块就是纯噪音（`BEGIN` 那张卡片的信息量是零）。但也不能凭空消失 ——
    用户写了 3 条只看到 2 条，会以为漏采。所以收进每组的折叠行里。
    """
    reason = ""
    for f in st["findings"]:
        if f["code"] in _BOOKKEEPING:
            reason = f["detail"]
            break
    return {
        "no": st["no"],
        "sql": _short_sql(st["original"]),
        "status": st["status"],
        "status_label": st["status_label"],
        "reason": reason,
    }


def _skipped_hint(skipped: list) -> str:
    """折叠行的标题。两种原因的措辞不能混：它们是不同的事。"""
    if not skipped:
        return ""
    names = "、".join(s["sql"] for s in skipped)
    kinds = {s["status"] for s in skipped}
    n = len(skipped)
    if kinds == {"skipped_not_explainable"}:
        return f"另有 {n} 条语句不产生执行计划（事务控制、SET 等）：{names}"
    if kinds == {"not_probed"}:
        return f"另有 {n} 条语句超出本轮采集额度，未采集：{names}"
    return f"另有 {n} 条语句未采集：{names}"


def _headline(summary: dict, ok: bool, error: Optional[str]) -> dict:
    """一句话结论。口径与 `worst_level` 完全一致。"""
    worst = summary.get("worst_level")
    probed = summary.get("probed") or 0
    by_code = summary.get("by_code") or {}
    # 只数"计划质量问题"，不含"跳过了 BEGIN"这类事实记录
    quality = sum(v for k, v in by_code.items() if k not in _BOOKKEEPING)
    total = summary.get("statements") or 0

    if not ok:
        return {
            "cls": "advice-info", "title": "采集未完成",
            "detail": error or "未能连接到目标库，本轮没有拿到执行计划。压测本身不受影响。",
        }
    if probed == 0:
        return {
            "cls": "advice-info", "title": "没有语句被采集",
            "detail": "提交的语句都不可 EXPLAIN（如只有事务控制语句），或全部超出了采集额度。",
        }
    if worst is None:
        return {
            "cls": "advice-ok", "title": "计划健康",
            "detail": f"被采集的 {probed} 条语句都没有坏味道（共 {total} 条语句）。",
        }
    return {
        "cls": "advice-error" if worst == "error" else "advice-warn",
        "title": f"发现 {quality} 处计划问题",
        "detail": f"被采集的 {probed} 条语句中有 {quality} 处值得关注。"
                  f"这些是计划层面的原因：压测测出的耗时说明「哪里慢」，"
                  f"这里说明「为什么慢」。",
    }


def build_view(artifact: Optional[dict], run: Optional[dict] = None) -> dict:
    """产物 → 模板视图模型。`artifact` 为 None 时返回可渲染的空态（不抛异常）。"""
    run_id = (artifact or {}).get("run_id") or (run or {}).get("id") or ""
    if not artifact:
        return {
            "available": False,
            "run_id": run_id,
            "run_name": (run or {}).get("name"),
            "hint": "本轮压测还没有可用的执行计划产物。可能原因：采集被配置关闭"
                    "（EXPLAIN_PROBE_ENABLED=false）、压测进程异常退出导致收尾没走到"
                    "（服务重启、进程被强杀，孤儿 run 会被直接标 failed）、"
                    "或 data/explain/ 下的产物已被清理。",
        }

    summary = artifact.get("summary") or {}
    max_rows_ref = summary.get("max_rows_ref") or {}
    ref_text = ""
    if max_rows_ref.get("sql_id"):
        ref_text = f"{max_rows_ref['sql_id']} 第 {max_rows_ref.get('index', 0) + 1} 条"

    stats = [
        {"label": "已采集语句", "value": f"{summary.get('probed') or 0} / {summary.get('statements') or 0}"},
        {"label": "预估最大扫描行数", "value": _fmt_rows(summary.get("max_rows")) if summary.get("max_rows") is not None else "—",
         "sub": ref_text},
        {"label": "目标库", "value": _fmt_target(artifact.get("target") or {})},
        {"label": "MySQL 版本", "value": artifact.get("server_version") or "—"},
        {"label": "采集耗时", "value": f"{summary.get('elapsed_ms')} ms" if summary.get("elapsed_ms") is not None else "—"},
    ]

    tasks = []
    for t in artifact.get("tasks") or []:
        entries = list(t.get("statements") or [])
        all_statements = [_statement_view(e, i + 1) for i, e in enumerate(entries)]
        # `statements` 保持"产物里有什么就有什么"（既有契约，测试锁着）；
        # `shown` / `skipped` 是给模板用的切分：bookkeeping 类不占卡片位。
        skipped = [_skipped_view(s) for s in all_statements if s["status"] in _BOOKKEEPING]
        tasks.append({
            "sql_id": t.get("sql_id"),
            "weight": t.get("weight"),
            "statements": all_statements,
            "shown": [s for s in all_statements if s["status"] not in _BOOKKEEPING],
            "skipped": skipped,
            "skipped_hint": _skipped_hint(skipped),
        })

    return {
        "available": True,
        "run_id": run_id,
        "run_name": (run or {}).get("name"),
        "ok": bool(artifact.get("ok")),
        "error": artifact.get("error"),
        "generated_at": artifact.get("generated_at"),
        "phase": artifact.get("phase"),
        "probe_version": artifact.get("probe_version"),
        "headline": _headline(summary, bool(artifact.get("ok")), artifact.get("error")),
        "stats": stats,
        "notes": list(artifact.get("notes") or []),
        "truncated": bool(artifact.get("truncated")),
        "truncated_reason": artifact.get("truncated_reason"),
        "summary": summary,
        "tasks": tasks,
    }
