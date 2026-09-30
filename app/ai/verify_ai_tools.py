"""SQLPulse 只读诊断工具验证入口（PASS / WARN / FAIL）。

用法：
    python -m app.ai.verify_ai_tools --run-id <任务ID> [--tool context|metrics|explain|table_profile]
                                     [--table 表名] [--data-dir DIR] [--verbose]

- --run-id   必填：8 位十六进制任务 ID（仓库真实规则：uuid4().hex[:8]，如 bb01ea65），
             传入值原样交给工具，大小写/首尾空格由工具自己归一化。
- --tool     可选：只验证单个工具；缺省验证 context / metrics / explain 三个。
- --table    可选：目标库表名；给出时会额外验证 get_table_profile。
             这是本入口唯一会**连接目标 MySQL** 的检查（工具侧还要求 AI_DB_PROBE_ENABLED
             已开启，否则只会得到 DB_PROBE_DISABLED）。
- --data-dir 可选：只读数据目录（须含 sqlpulse.db、explain/、locust/）；缺省用应用配置的 data_dir。
- --verbose  可选：额外输出工具的完整返回（默认只输出检查摘要，不含 SQL、变量与执行计划）。

检查分级：
    PASS  返回结构（ok/run_id/data/warnings/error）与字段语义检查通过。
    WARN  工具**正确报告**了数据缺失、尚未就绪或部分采集（不是失败）。
    FAIL  内部异常、结构不符、状态自相矛盾，或用户指定的真实任务不存在。

退出码：无 FAIL 为 0，有 FAIL 为 1，无法导入应用模块等环境错误为 2。
注意：PASS 只说明结构与语义没问题，**不代表诊断证据齐全**——证据缺口一律以 WARN 列出。

行为约束：只读，不启动/停止压测、不造数、不连接目标 MySQL、不执行 EXPLAIN、
不初始化 SQLite、不生成报告、不写数据文件；不 import app.main；不调用模型。
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_ENVELOPE_KEYS = ("ok", "run_id", "data", "warnings", "error")

_SUMMARY_COUNTS = ("total_requests", "total_failures")
_SUMMARY_NUMS = (
    "avg_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms", "qps",
    "qps_avg", "qps_peak", "threads_running_p95",
    "mysql_slow_total", "mysql_lock_waits_total",
)
_PER_SQL_COUNTS = ("total_requests", "total_failures")
_PER_SQL_NUMS = ("avg_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms", "qps")
_STATEMENT_COUNTERS = (
    "probed_ok", "skipped_not_explainable", "compile_error", "explain_error",
    "not_probed", "other_failed",
)

# 工具错误码 → 检查等级（针对"用户指定的真实任务"）
_ERROR_LEVEL = {
    "RUN_NOT_FOUND": FAIL, "INVALID_RUN_ID": FAIL, "DATA_INVALID": FAIL,
    "DATA_UNAVAILABLE": FAIL, "TOOL_INTERNAL_ERROR": FAIL, "EXPLAIN_CORRUPT": FAIL,
    "EXPLAIN_NOT_READY": WARN, "EXPLAIN_NOT_AVAILABLE": WARN,
    # 目标库元数据：能力未开或目标库侧不可用都算"工具正确报告状态"，
    # 但表是用户点名要看的，找不到就是失败。
    "DB_PROBE_DISABLED": WARN, "DB_CONNECT_FAILED": WARN, "DB_TIMEOUT": WARN,
    "DB_PERMISSION_DENIED": WARN, "DB_QUERY_FAILED": WARN,
    "TABLE_NOT_FOUND": FAIL, "INVALID_TABLE_NAME": FAIL,
}
# 这些码表示"目标库元数据检查没跑成"，结构未被验证
_PROFILE_DEGRADED = {"DB_PROBE_DISABLED", "DB_CONNECT_FAILED", "DB_TIMEOUT",
                     "DB_PERMISSION_DENIED", "DB_QUERY_FAILED"}


def _c(checks: list, name: str, level: str, detail: str = "") -> None:
    checks.append({"name": name, "level": level, "detail": detail})


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_rate(v) -> bool:
    return _is_num(v) and 0.0 <= v <= 1.0


def _replace_nonfinite(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _replace_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_nonfinite(v) for v in obj]
    return obj


def _dump(obj) -> tuple:
    """严格序列化（allow_nan=False）；出现非有限浮点数时降级为替换 null 后重试。"""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), False
    except ValueError:
        return json.dumps(_replace_nonfinite(obj), ensure_ascii=False, indent=2,
                          allow_nan=False), True


def _call(fn, value) -> dict:
    """调用工具并把意外异常包成错误信封（工具本身不应抛异常）。"""
    try:
        result = fn(value)
    except Exception as e:  # noqa: BLE001 验证入口的兜底边界
        return {
            "ok": False, "run_id": None, "data": None, "warnings": [],
            "error": {"code": "VERIFIER_TOOL_RAISED", "retryable": False,
                      "message": f"{type(e).__name__}: {e}"},
        }
    return result


# ---------------------------------------------------------------------------
# 结构检查
# ---------------------------------------------------------------------------

def check_envelope(checks: list, label: str, result) -> bool:
    """检查统一的 {ok, run_id, data, warnings, error} 结构；成功与失败分别校验。"""
    if not isinstance(result, dict):
        _c(checks, f"{label}.envelope", FAIL, f"返回不是对象：{type(result).__name__}")
        return False
    missing = [k for k in _ENVELOPE_KEYS if k not in result]
    if missing:
        _c(checks, f"{label}.envelope", FAIL, f"缺少字段：{', '.join(missing)}")
        return False
    ok = result["ok"]
    if not isinstance(ok, bool):
        _c(checks, f"{label}.envelope", FAIL, f"ok 不是布尔：{type(ok).__name__}")
        return False
    if not isinstance(result["warnings"], list):
        _c(checks, f"{label}.envelope", FAIL, "warnings 不是列表")
        return False

    if ok:
        if result["error"] is not None:
            _c(checks, f"{label}.envelope", FAIL, "ok=true 但 error 非 null")
            return False
        if not isinstance(result["data"], dict):
            _c(checks, f"{label}.envelope", FAIL, "ok=true 但 data 不是对象")
            return False
        if not isinstance(result["run_id"], str):
            _c(checks, f"{label}.envelope", FAIL, "ok=true 但 run_id 不是字符串")
            return False
        _c(checks, f"{label}.envelope", PASS, "成功结构 ok/run_id/data/warnings/error 齐全")
        return True

    if result["data"] is not None:
        _c(checks, f"{label}.envelope", FAIL, "ok=false 但 data 非 null")
        return False
    err = result["error"]
    if not isinstance(err, dict) or not isinstance(err.get("code"), str) \
            or not isinstance(err.get("message"), str) or not isinstance(err.get("retryable"), bool):
        _c(checks, f"{label}.envelope", FAIL, "ok=false 但 error 不是 {code,message,retryable}")
        return False
    if result["run_id"] is not None and not isinstance(result["run_id"], str):
        _c(checks, f"{label}.envelope", FAIL, "run_id 既不是字符串也不是 null")
        return False
    _c(checks, f"{label}.envelope", PASS, f"失败结构齐全（code={err['code']}）")
    return True


# ---------------------------------------------------------------------------
# 各工具的语义检查
# ---------------------------------------------------------------------------

def check_context(checks: list, data: dict) -> None:
    for key, kind in (("name", str), ("status", str), ("sql_source", str), ("sql_content", str)):
        if not isinstance(data.get(key), kind):
            _c(checks, f"context.{key}", FAIL, f"{key} 不是字符串")
            return
    if not isinstance(data.get("groups"), list) or not isinstance(data.get("variables"), dict):
        _c(checks, "context.groups/variables", FAIL, "groups 必须为列表、variables 必须为对象")
        return
    config = data.get("config")
    if not isinstance(config, dict) or any(not _is_count(config.get(k)) for k in
                                           ("concurrency", "spawn_rate", "duration_sec")):
        _c(checks, "context.config", FAIL, "config 的并发/速率/时长必须是非负整数")
        return
    _c(checks, "context.config", PASS,
       f"concurrency={config['concurrency']} spawn_rate={config['spawn_rate']} "
       f"duration_sec={config['duration_sec']}")

    times = data.get("times")
    if not isinstance(times, dict) or any(
            times.get(k) is not None and not isinstance(times.get(k), str)
            for k in ("created_at", "started_at", "ended_at")):
        _c(checks, "context.times", FAIL, "times 的时间字段必须是字符串或 null")
    else:
        _c(checks, "context.times", PASS, f"ended_at={times.get('ended_at')}")

    if data.get("error_msg") is not None and not isinstance(data.get("error_msg"), str):
        _c(checks, "context.error_msg", FAIL, "error_msg 必须是字符串或 null")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        _c(checks, "context.tasks", FAIL, "tasks 必须是非空列表")
        return
    ids: list = []
    for t in tasks:
        if not isinstance(t, dict) or not isinstance(t.get("sql_id"), str):
            _c(checks, "context.tasks", FAIL, "任务缺少 sql_id 字符串")
            return
        if not (t.get("weight") is None or _is_count(t["weight"])):
            _c(checks, "context.tasks", FAIL, f"{t['sql_id']} 的 weight 必须是非负整数或 null")
            return
        statements = t.get("statements")
        if not isinstance(statements, list) or not statements \
                or any(not isinstance(s, str) for s in statements):
            _c(checks, "context.tasks", FAIL, f"{t['sql_id']} 的 statements 必须是非空字符串列表")
            return
        ids.append(t["sql_id"])
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"sql_\d+", i) for i in ids):
        _c(checks, "context.tasks", FAIL, f"sql_id 必须唯一且形如 sql_N：{ids}")
        return
    _c(checks, "context.tasks", PASS, f"{len(ids)} 个任务组：{', '.join(ids)}")

    status = data.get("status")
    if status in ("pending", "running"):
        _c(checks, "context.status", WARN, f"任务状态为 {status}：配置与 SQL 已知，结果尚未产生")


def _check_numbers(checks: list, label: str, holder: dict, counts, nums, rates) -> bool:
    bad: list = []
    for key in counts:
        if key not in holder:
            bad.append(f"{key}(缺失)")
        elif holder[key] is not None and not _is_count(holder[key]):
            bad.append(f"{key}={holder[key]!r}")
    for key in nums:
        if key not in holder:
            bad.append(f"{key}(缺失)")
        elif holder[key] is not None and not _is_num(holder[key]):
            bad.append(f"{key}={holder[key]!r}")
    for key in rates:
        if key not in holder:
            bad.append(f"{key}(缺失)")
        elif holder[key] is not None and not _is_rate(holder[key]):
            bad.append(f"{key}={holder[key]!r}")
    if bad:
        _c(checks, label, FAIL, "字段类型/范围不符：" + "；".join(bad))
        return False
    _c(checks, label, PASS, "计数为非负整数或 null、延迟/QPS 有限或 null、错误率在 0~1 或 null")
    return True


def check_metrics(checks: list, data: dict) -> dict:
    summary, sources = data.get("summary"), data.get("summary_sources")
    if not isinstance(summary, dict) or not isinstance(sources, dict):
        _c(checks, "metrics.summary", FAIL, "summary 或 summary_sources 不是对象")
        return data
    uncovered = sorted(set(summary) - set(sources))
    if uncovered:
        _c(checks, "metrics.summary_sources", FAIL, f"未覆盖字段：{', '.join(uncovered)}")
    else:
        used = sorted({v for v in sources.values() if v is not None})
        _c(checks, "metrics.summary_sources", PASS,
           f"覆盖 summary 全部 {len(summary)} 个字段；来源：{', '.join(used) or '无（全部为 null）'}")

    _check_numbers(checks, "metrics.summary", summary,
                   _SUMMARY_COUNTS, _SUMMARY_NUMS, ("err_rate",))

    empty = [k for k, v in summary.items() if v is None]
    if data.get("missing") != empty:
        _c(checks, "metrics.missing", FAIL, "missing 与 summary 中的 null 字段不一致")
    elif empty:
        _c(checks, "metrics.missing", WARN, f"{len(empty)} 个字段无数据：{', '.join(empty)}")
    else:
        _c(checks, "metrics.missing", PASS, "summary 无缺失字段")

    if not isinstance(data.get("data_available"), bool) or not isinstance(data.get("in_progress"), bool):
        _c(checks, "metrics.flags", FAIL, "data_available / in_progress 必须是布尔")

    l1 = data.get("l1_findings")
    if l1 is None:
        _c(checks, "metrics.l1_findings", WARN, "没有已保存的 L1（无报告或报告未生成）")
    elif not isinstance(l1, list) or any(not isinstance(i, dict) for i in l1):
        _c(checks, "metrics.l1_findings", FAIL, "l1_findings 必须是对象列表或 null")
    else:
        _c(checks, "metrics.l1_findings", PASS, f"{len(l1)} 条规则结论")

    created = data.get("report_created_at")
    if created is None:
        _c(checks, "metrics.report_created_at", WARN, "没有已保存报告的时间戳")
    elif not isinstance(created, str):
        _c(checks, "metrics.report_created_at", FAIL, "report_created_at 必须是字符串或 null")
    else:
        _c(checks, "metrics.report_created_at", PASS, created)

    per_sql = data.get("per_sql")
    if not isinstance(per_sql, dict):
        _c(checks, "metrics.per_sql", FAIL, "per_sql 不是对象")
        return data
    if per_sql and not isinstance(data.get("per_sql_source"), str):
        _c(checks, "metrics.per_sql_source", FAIL, "per_sql 非空时 per_sql_source 必须是字符串")
    bad = [k for k in per_sql if not re.fullmatch(r"sql_\d+", str(k))]
    if bad:
        _c(checks, "metrics.per_sql", FAIL, f"sql_id 命名不符：{bad}")
    for sql_id, entry in per_sql.items():
        if not isinstance(entry, dict):
            _c(checks, "metrics.per_sql", FAIL, f"{sql_id} 不是对象")
            break
        if not _check_numbers(checks, f"metrics.per_sql[{sql_id}]", entry,
                              _PER_SQL_COUNTS, _PER_SQL_NUMS, ("err_rate",)):
            break
    else:
        if per_sql:
            _c(checks, "metrics.per_sql", PASS, f"{len(per_sql)} 个任务组：{', '.join(sorted(per_sql))}")

    if data.get("in_progress"):
        _c(checks, "metrics.in_progress", WARN, "任务未结束，读到的是一次快照")
    return data


def check_explain(checks: list, data: dict) -> None:
    for key in ("probe_ok", "collected", "truncated"):
        if not isinstance(data.get(key), bool):
            _c(checks, f"explain.{key}", FAIL, f"{key} 必须是布尔")
            return
    status = data.get("collection_status")
    if status not in ("complete", "partial", "no_plans"):
        _c(checks, "explain.collection_status", FAIL, f"取值非法：{status!r}")
        return

    st = data.get("statement_status")
    if not isinstance(st, dict) or not _is_count(st.get("total")) \
            or any(not _is_count(st.get(k)) for k in _STATEMENT_COUNTERS):
        _c(checks, "explain.statement_status", FAIL, "statement_status 必须是非负整数计数")
        return
    _c(checks, "explain.statement_status", PASS,
       f"total={st['total']} probed_ok={st['probed_ok']} "
       f"skipped={st['skipped_not_explainable']} 其他失败="
       f"{st['compile_error'] + st['explain_error'] + st['not_probed'] + st['other_failed']}")

    accounted = st["probed_ok"] + sum(st[k] for k in _STATEMENT_COUNTERS if k != "probed_ok")
    if accounted != st["total"]:
        _c(checks, "explain.statement_accounting", FAIL,
           f"语句计数自相矛盾：{accounted} != total {st['total']}")

    if data["collected"] != (st["probed_ok"] > 0):
        _c(checks, "explain.collected", FAIL,
           f"collected={data['collected']} 与 probed_ok={st['probed_ok']} 不一致（应等于 probed_ok>0）")

    truncated = data["truncated"]
    if status == "complete" and (st["probed_ok"] != st["total"] or truncated):
        _c(checks, "explain.collection_status", FAIL, "complete 与成功语句数/截断标记不一致")
    elif status == "partial" and not (st["probed_ok"] > 0 and (st["probed_ok"] < st["total"] or truncated)):
        _c(checks, "explain.collection_status", FAIL, "partial 与成功语句数/截断标记不一致")
    elif status == "no_plans" and st["probed_ok"] != 0:
        _c(checks, "explain.collection_status", FAIL, "no_plans 却有成功语句")
    else:
        _c(checks, "explain.collection_status", PASS,
           f"{status}（成功 {st['probed_ok']}/{st['total']}，truncated={truncated}）")

    # probe_ok 只表示采集时连上了目标库，不等于拿到了计划
    if data["probe_ok"] and st["probed_ok"] == 0:
        _c(checks, "explain.probe_ok", WARN, "probe_ok=true 但没有任何语句取得执行计划")
    if truncated:
        _c(checks, "explain.truncated", WARN, f"采集被截断：{data.get('truncated_reason')}")
    if status == "partial":
        _c(checks, "explain.partial", WARN, "只有部分语句取得计划")
    if status == "no_plans":
        _c(checks, "explain.no_plans", WARN, "没有取得任何执行计划（全部不可 EXPLAIN 或全部未采集）")
    if st["total"] == 0:
        _c(checks, "explain.empty", WARN, "产物没有任何语句条目")

    for key in ("probe_version", "phase", "generated_at", "server_version"):
        if data.get(key) is not None and not isinstance(data.get(key), (str, int)):
            _c(checks, f"explain.{key}", FAIL, f"{key} 类型不符")
    target = data.get("target")
    if target is not None and not isinstance(target, dict):
        _c(checks, "explain.target", FAIL, "target 必须是对象或 null")
    elif isinstance(target, dict) and any(k not in target for k in ("host", "port", "database", "user")):
        _c(checks, "explain.target", FAIL, "target 缺少白名单字段")
    if isinstance(data.get("probe_error"), str) and data["probe_error"]:
        _c(checks, "explain.probe_error", WARN, "产物记录了采集错误（详见 --verbose）")


def check_table_profile(checks: list, data: dict, table: str) -> None:
    """get_table_profile 的语义检查：结构、类型，以及"密码绝不外发"。"""
    if data.get("table") != table:
        _c(checks, "profile.table", FAIL, f"回显表名 {data.get('table')!r}，期望 {table!r}")
    if not isinstance(data.get("database"), str) or not data["database"]:
        _c(checks, "profile.database", FAIL, "database 必须是非空字符串")
    for key in ("collected_at", "source"):
        if not isinstance(data.get(key), str) or not data[key]:
            _c(checks, f"profile.{key}", FAIL, f"{key} 必须是非空字符串")
    if not isinstance(data.get("run_status"), str):
        _c(checks, "profile.run_status", FAIL, "run_status 必须是字符串")

    target = data.get("target")
    if not isinstance(target, dict) or any(k not in target for k in ("host", "port", "database", "user")):
        _c(checks, "profile.target", FAIL, "target 必须是含 host/port/database/user 的对象")
    elif "password" in target or any(
            isinstance(v, str) and v.lower().startswith(("password", "pwd")) for v in target.values()):
        _c(checks, "profile.target", FAIL, "target 里出现了 password 字段")
    else:
        _c(checks, "profile.target", PASS, f"{target.get('host')}:{target.get('port')}/{target.get('database')}")

    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        _c(checks, "profile.columns", FAIL, "columns 必须是非空列表")
    elif any(not isinstance(c, dict) or not isinstance(c.get("name"), str)
             or not isinstance(c.get("data_type"), str) or not isinstance(c.get("nullable"), bool)
             for c in columns):
        _c(checks, "profile.columns", FAIL, "列条目缺少 name/data_type/nullable 或类型不符")
    else:
        _c(checks, "profile.columns", PASS, f"{len(columns)} 列，例：{columns[0]['name']}")

    indexes = data.get("indexes")
    if not isinstance(indexes, list):
        _c(checks, "profile.indexes", FAIL, "indexes 必须是列表")
    elif any(not isinstance(i, dict) or not isinstance(i.get("name"), str)
             or not isinstance(i.get("unique"), bool) or not isinstance(i.get("columns"), list)
             or not i["columns"]
             or any(not isinstance(c, dict) or not isinstance(c.get("name"), str) for c in i["columns"])
             for i in indexes):
        _c(checks, "profile.indexes", FAIL, "索引条目缺少 name/unique/columns 或类型不符")
    else:
        names = ", ".join(f"{i['name']}({'唯一' if i['unique'] else '普通'})" for i in indexes)
        _c(checks, "profile.indexes", PASS, f"{len(indexes)} 个索引：{names or '无'}")

    stats = data.get("table_stats")
    if not isinstance(stats, dict) or stats.get("estimated") is not True:
        _c(checks, "profile.table_stats", FAIL, "table_stats 必须是对象且显式标注 estimated=true（估算值）")
    elif stats.get("table_rows") is not None and not _is_count(stats["table_rows"]):
        _c(checks, "profile.table_stats", FAIL, "table_rows 必须是非负整数或 null")
    else:
        _c(checks, "profile.table_stats", PASS,
           f"engine={stats.get('engine')} table_rows≈{stats.get('table_rows')}（估算）")

    for key in ("caveats",):
        value = data.get(key)
        if not isinstance(value, list) or not value or any(not isinstance(x, str) for x in value):
            _c(checks, f"profile.{key}", FAIL, f"{key} 必须是非空字符串列表")
    if not isinstance(data.get("missing"), list):
        _c(checks, "profile.missing", FAIL, "missing 必须是列表")


# ---------------------------------------------------------------------------
# run_id 相关检查
# ---------------------------------------------------------------------------

def check_normalization(checks: list, tools: dict, requested: list, normalized: str,
                        results: dict) -> None:
    """用大写+前后空格的版本再调一次，确认被识别为同一个任务。

    不要求两次的数据完全相等（运行中的数据会变），只比对规范化后的 run_id；
    主调用本身失败时（不存在/未就绪/产物损坏），只要求两次的错误码一致，
    以证明归一化没有把同一个 ID 变成另一种解析结果。
    """
    variant = f"  {normalized.upper()}  "
    for name in requested:
        primary = results.get(name) or {}
        result = _call(tools[name], variant)
        if not check_envelope(checks, f"norm.{name}", result):
            return

        if primary.get("ok"):
            if result["ok"]:
                got = (result["data"] or {}).get("run_id")
                if got == normalized:
                    _c(checks, f"norm.{name}", PASS, f"{variant!r} → {got}")
                else:
                    _c(checks, f"norm.{name}", FAIL,
                       f"{variant!r} 识别为 {got!r}，应为 {normalized!r}")
            else:
                vcode = result["error"]["code"]
                level = WARN if vcode in ("EXPLAIN_NOT_READY", "EXPLAIN_NOT_AVAILABLE") else FAIL
                _c(checks, f"norm.{name}", level,
                   f"原文输入成功，{variant!r} 返回 {vcode}（状态可能在两次调用之间变化）")
            continue

        pcode = (primary.get("error") or {}).get("code")
        vcode = None if result["ok"] else result["error"]["code"]
        if vcode == "INVALID_RUN_ID":
            _c(checks, f"norm.{name}", FAIL,
               f"{variant!r} 被判为 INVALID_RUN_ID，归一化未生效")
        elif result["ok"] or vcode == pcode:
            _c(checks, f"norm.{name}", WARN,
               f"归一化正常（两次结果一致：{pcode}），但该任务不可读取")
        else:
            _c(checks, f"norm.{name}", FAIL,
               f"原文输入 {pcode}，{variant!r} 返回 {vcode}，两次不一致")


def check_invalid_id(checks: list, tools: dict, requested: list) -> None:
    for name in requested[:1] or ["context"]:
        for bad in ("../etc/passwd", "ZZZZZZZZ"):
            result = _call(tools[name], bad)
            if not check_envelope(checks, f"invalid.{name}", result):
                return
            code = (result.get("error") or {}).get("code")
            if result["ok"]:
                _c(checks, f"invalid.{name}", FAIL, f"{bad!r} 未被拒绝")
            elif code == "INVALID_RUN_ID":
                _c(checks, f"invalid.{name}", PASS, f"{bad!r} → INVALID_RUN_ID")
            else:
                _c(checks, f"invalid.{name}", FAIL, f"{bad!r} 返回 {code}，应为 INVALID_RUN_ID")


def run_exists(db_path: Path, run_id: str) -> Optional[bool]:
    """只读查一次 runs 表；库文件不存在时返回 None（无法判定）。"""
    if not db_path.is_file():
        return None
    uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def check_absent_id(checks: list, tools: dict, requested: list, db_path: Path) -> None:
    """检查一个确认不存在的 ID：先核实它确实不在库中，再看工具是否报 RUN_NOT_FOUND。"""
    candidate = uuid.uuid4().hex[:8]
    exists = run_exists(db_path, candidate)
    if exists is None:
        _c(checks, "absent.id", WARN, "无法读取 runs 表，未检查不存在的 ID")
        return
    if exists:
        _c(checks, "absent.id", WARN, f"随机 ID {candidate} 恰好存在，本次跳过该检查")
        return
    for name in requested:
        result = _call(tools[name], candidate)
        if not check_envelope(checks, f"absent.{name}", result):
            return
        code = (result.get("error") or {}).get("code")
        if result["ok"]:
            _c(checks, f"absent.{name}", FAIL, f"{candidate} 不存在却返回成功")
        elif code == "RUN_NOT_FOUND":
            _c(checks, f"absent.{name}", PASS, f"{candidate} 已核实不在库中 → RUN_NOT_FOUND")
        elif code in _PROFILE_DEGRADED:
            _c(checks, f"absent.{name}", WARN, f"{name} 返回 {code}（能力未开/目标库不可用），未验证该检查")
        else:
            _c(checks, f"absent.{name}", FAIL, f"{candidate} 返回 {code}，应为 RUN_NOT_FOUND")


def check_sql_ids(checks: list, results: dict) -> None:
    """三个工具对同一 run 的 sql_id 集合是否自洽（方向敏感）。"""
    def ids_of(name: str):
        result = results.get(name) or {}
        if not result.get("ok"):
            return None
        data = result.get("data") or {}
        if name == "metrics":
            return set((data.get("per_sql") or {}).keys())
        return {t.get("sql_id") for t in (data.get("tasks") or []) if isinstance(t, dict)}

    ctx = ids_of("context")
    if ctx is None:
        return
    for name in ("metrics", "explain"):
        other = ids_of(name)
        if other is None:
            _c(checks, f"ids.context/{name}", WARN, f"{name} 未成功返回，未能核对 sql_id")
            continue
        unknown = sorted(other - ctx)
        missing = sorted(ctx - other)
        if unknown:
            _c(checks, f"ids.context/{name}", FAIL, f"{name} 出现 context 中不存在的 sql_id：{unknown}")
        elif missing:
            _c(checks, f"ids.context/{name}", WARN, f"{name} 缺少 {missing}（未上报或未采集）")
        else:
            _c(checks, f"ids.context/{name}", PASS, f"sql_id 一致：{', '.join(sorted(ctx))}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ai.verify_ai_tools",
        description="验证三个只读诊断工具：结构、字段语义、run_id 归一化与缺失场景",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0=无 FAIL；1=有 FAIL；2=环境/依赖错误",
    )
    parser.add_argument("--run-id", required=True, metavar="RUN_ID",
                        help="8 位十六进制任务 ID（如 bb01ea65）")
    parser.add_argument("--tool", choices=("context", "metrics", "explain", "table_profile"),
                        default=None,
                        help="只验证一个工具；缺省验证 context/metrics/explain 三个")
    parser.add_argument("--table", metavar="TABLE", default=None,
                        help="目标库表名；给出时额外验证 get_table_profile（会连目标库）")
    parser.add_argument("--data-dir", metavar="DIR", default=None,
                        help="只读数据目录（含 sqlpulse.db）；缺省用应用配置的 data_dir")
    parser.add_argument("--verbose", action="store_true",
                        help="额外输出工具的完整返回（默认只给检查摘要）")
    args = parser.parse_args(argv)
    if args.tool == "table_profile" and not args.table:
        parser.error("--tool table_profile 需要同时给出 --table 表名")

    # 必须在任何 app.* import 之前设置：Settings 在 import 时实例化。
    if args.data_dir:
        os.environ["DATA_DIR"] = os.path.abspath(args.data_dir)
    if "APP_SECRET_KEY" not in os.environ:
        # app.config 在 AUTH_ENABLED=true 时要求 APP_SECRET_KEY；无密钥环境需先关掉。
        os.environ.setdefault("AUTH_ENABLED", "false")

    checks: list = []
    profile_degraded: list = []
    try:
        from app.ai.tools import (get_run_context, get_run_explain, get_run_metrics,
                                  get_table_profile)
        from app.config import settings
    except Exception as e:
        payload = {"run_id_input": args.run_id, "data_dir": args.data_dir,
                   "checks": [{"name": "cli.import", "level": FAIL,
                               "detail": f"无法导入应用模块：{type(e).__name__}: {e}"}],
                   "exit_code": 2}
        print(_dump(payload)[0])
        print("退出码 2：环境/依赖错误", file=sys.stderr)
        return 2

    tools = {"context": get_run_context, "metrics": get_run_metrics, "explain": get_run_explain,
             "table_profile": lambda rid: get_table_profile(rid, args.table)}
    requested = [args.tool] if args.tool else ["context", "metrics", "explain"]
    if args.table and "table_profile" not in requested:
        requested.append("table_profile")
    normalized = args.run_id.strip().lower()
    valid = bool(re.fullmatch(r"[0-9a-f]{8}", normalized))

    results: dict = {}
    if not valid:
        _c(checks, "run_id.format", FAIL,
           f"--run-id={args.run_id!r} 不是 8 位十六进制，无法验证真实任务")
    else:
        for name in requested:
            result = _call(tools[name], args.run_id)
            results[name] = result
            if not check_envelope(checks, name, result):
                continue
            if result["ok"]:
                data = result["data"]
                if data.get("run_id") != normalized:
                    _c(checks, f"{name}.run_id", FAIL,
                       f"返回 run_id={data.get('run_id')!r}，应为 {normalized!r}")
                else:
                    _c(checks, f"{name}.run_id", PASS, normalized)
                if name == "context":
                    check_context(checks, data)
                elif name == "metrics":
                    check_metrics(checks, data)
                elif name == "table_profile":
                    check_table_profile(checks, data, args.table)
                else:
                    check_explain(checks, data)
                if result["warnings"]:
                    _c(checks, f"{name}.warnings", WARN, "；".join(str(w) for w in result["warnings"]))
                else:
                    _c(checks, f"{name}.warnings", PASS, "无警告")
            else:
                code = result["error"]["code"]
                level = _ERROR_LEVEL.get(code, FAIL)
                detail = result["error"]["message"]
                if code == "RUN_NOT_FOUND":
                    detail = f"用户指定的任务不存在：{normalized}"
                _c(checks, f"{name}.error", level, f"{code}：{detail}")
                if code in _PROFILE_DEGRADED:
                    profile_degraded.append(code)

        check_normalization(checks, tools, requested, normalized, results)
        check_sql_ids(checks, results)

    check_invalid_id(checks, tools, requested)
    check_absent_id(checks, tools, requested, Path(settings.data_dir) / "sqlpulse.db")

    db_path = Path(settings.data_dir) / "sqlpulse.db"
    payload = {
        "run_id_input": args.run_id,
        "normalized_run_id": normalized if valid else None,
        "data_dir": str(settings.data_dir),
        "sqlpulse_db": {"path": str(db_path), "exists": db_path.is_file()},
        "tools_requested": requested,
        "checks": checks,
        "note": "PASS 只表示结构与语义检查通过，不代表诊断证据齐全；数据缺口见 WARN。",
    }
    if args.table:
        payload["table"] = args.table
    if profile_degraded:
        payload["notes"] = [
            "目标库元数据检查未完成（" + ", ".join(sorted(set(profile_degraded))) +
            "）：get_table_profile 的结构与内容未被验证。"
        ]
    if args.verbose:
        payload["tool_results"] = results

    # 序列化自检（allow_nan=False）：不过就替换非有限浮点数并记 FAIL，再补计数与退出码
    _, sanitized = _dump(payload)
    if sanitized:
        payload = _replace_nonfinite(payload)
        _c(checks, "output.serializable", FAIL, "结果含非有限浮点数（NaN/Inf），已替换为 null")
    else:
        _c(checks, "output.serializable", PASS, "allow_nan=False 严格序列化通过")

    counts = {level: sum(1 for c in checks if c["level"] == level) for level in (PASS, WARN, FAIL)}
    exit_code = 1 if counts[FAIL] else 0
    payload["level_counts"] = counts
    payload["exit_code"] = exit_code

    text, still_bad = _dump(payload)
    if still_bad:  # 理论上不会发生：上面已把非有限值替换掉
        exit_code = 1
        payload["exit_code"] = exit_code
        text, _ = _dump(_replace_nonfinite(payload))
        print("注意：结果含非有限浮点数，已替换为 null", file=sys.stderr)

    print(text)
    print(f"PASS={counts[PASS]} WARN={counts[WARN]} FAIL={counts[FAIL]}；"
          f"退出码 {exit_code}；数据目录：{settings.data_dir}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
