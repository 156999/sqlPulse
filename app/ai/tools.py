"""SQLPulse 只读诊断工具。

前四个工具（``get_run_context`` / ``get_run_metrics`` / ``get_run_failures`` /
``get_run_explain``）只读平台数据：平台 SQLite（含逐秒采样的 metrics 表）、
Locust CSV 与 EXPLAIN 产物，**不连目标 MySQL**。
``get_table_profile`` 是唯一例外：按需只读目标库的元数据（列 / 索引 / 表规模估算），
必须显式开启 ``AI_DB_PROBE_ENABLED``，且连接只从 run 快照解析、不接受外部指定。

公共约束：只读、不写文件与库、run_id 校验挡路径穿越、统一信封
``{ok, run_id, data, warnings, error}``，错误对象 ``{code, message, retryable}``。
"""
import csv
import json
import math
import re
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from app.ai import db_profile
from app.config import settings
from app.services.report import read_stats
from app.services.explain_probe import PHASE_POST_RUN, artifact_path, load_artifact
from app.services.runner import resolve_tasks

# ---------------------------------------------------------------------------
# 公共：run_id 校验与只读访问
# ---------------------------------------------------------------------------

# 仓库真实 ID 规则：routers/tasks.py 用 uuid4().hex[:8]（8 位十六进制）。
# 校验同时挡住路径穿越（run_id 会进入 explain 产物文件名）。
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# runs 表字段白名单。db_dsn_json 有意缺席：它含目标库密码。
_RUN_COLS = (
    "id", "name", "status", "sql_source", "sql_content",
    "variables_json", "groups_json",
    "concurrency", "spawn_rate", "duration_sec",
    "error_msg", "created_at", "started_at", "ended_at",
)

# 错误文本里常见的凭据形态，兜底清洗（pymysql 报错本身不含密码，但
# locust 日志尾巴等文本可能夹带，error_msg 会进数据，先清洗再返回）。
_CRED_RE = re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*[^\s,;]+")


class _ToolError(Exception):
    """工具内部的预期错误：code/message/retryable 与输出错误对象一致。"""

    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _ok(run_id: str, data: dict, warnings: Optional[list] = None) -> dict:
    return {
        "ok": True,
        "run_id": run_id,
        "data": _sanitize(data),
        "warnings": list(warnings or []),
        "error": None,
    }


def _fail(run_id: str, code: str, message: str, retryable: bool) -> dict:
    return {
        "ok": False,
        "run_id": run_id,
        "data": None,
        "warnings": [],
        "error": {"code": code, "message": _redact(message), "retryable": retryable},
    }


def _sanitize(value):
    if isinstance(value, dict):
        return {key: "***" if str(key).lower() in
                {"password", "passwd", "pwd", "api_key", "authorization", "db_dsn_json"}
                else _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return _redact(value)


def _redact(text):
    if not isinstance(text, str):
        return text
    return _CRED_RE.sub(r"\1=***", text)


def _norm_run_id(run_id) -> Optional[str]:
    """归一化 run_id；非法输入返回 None。ID 由 uuid4().hex 生成，统一转小写。"""
    if not isinstance(run_id, str):
        return None
    value = run_id.strip().lower()
    return value if _RUN_ID_RE.fullmatch(value) else None


@contextmanager
def _readonly_conn():
    """打开平台 SQLite 的独立只读连接；库文件缺失时明确报错，不创建。"""
    path = settings.data_dir / "sqlpulse.db"
    if not path.is_file():
        raise _ToolError(
            "DATA_UNAVAILABLE",
            f"平台数据库不存在：{path}（缺少数据时明确报错，不自动创建空库）",
            False,
        )
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as e:
        raise _ToolError(
            "DATA_UNAVAILABLE",
            f"平台数据库无法以只读方式打开：{type(e).__name__}: {e}",
            False,
        ) from e
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _fetch_run_dsn(run_id: str) -> Optional[dict]:
    """单独读取 runs.db_dsn_json（含目标库密码）。

    这是 runs 字段白名单的**唯一例外**，只为 get_table_profile 建连使用：
    返回值绝不进入 data / warnings / error，也不写日志；调用方用完即弃。
    """
    with _readonly_conn() as conn:
        try:
            row = conn.execute("SELECT db_dsn_json FROM runs WHERE id=?", (run_id,)).fetchone()
        except sqlite3.Error as e:
            raise _ToolError(
                "DATA_INVALID", f"读取 runs 表失败：{type(e).__name__}: {e}", False
            ) from e
    if row is None:
        return None
    try:
        dsn = json.loads(row["db_dsn_json"])
    except (ValueError, TypeError):
        raise _ToolError("DATA_INVALID", "任务的目标库连接信息无法解析", False)
    if not isinstance(dsn, dict):
        raise _ToolError("DATA_INVALID", "任务的目标库连接信息结构不正确", False)
    return dsn


def _fetch_run(run_id: str) -> Optional[dict]:
    with _readonly_conn() as conn:
        try:
            row = conn.execute(
                f"SELECT {', '.join(_RUN_COLS)} FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        except sqlite3.Error as e:
            raise _ToolError(
                "DATA_INVALID", f"读取 runs 表失败：{type(e).__name__}: {e}", False
            ) from e
    return dict(row) if row else None


def _wrap(run_id: str, fn):
    """统一异常边界：预期错误按 code 返回，未知异常标 TOOL_INTERNAL_ERROR。"""
    try:
        return fn(run_id)
    except _ToolError as e:
        return _fail(run_id, e.code, e.message, e.retryable)
    except Exception as e:  # noqa: BLE001 工具边界：任何意外都不能伪装成"任务不存在"
        return _fail(
            run_id,
            "TOOL_INTERNAL_ERROR",
            _redact(f"工具内部错误：{type(e).__name__}: {e}"),
            False,
        )


# ---------------------------------------------------------------------------
# 工具 1：get_run_context
# ---------------------------------------------------------------------------

def get_run_context(run_id: str) -> dict:
    """回答「这次压测测了什么」。

    返回任务参数、时间、SQL 来源、原始完整 SQL、任务组（SQL ID / 名称 / 权重 /
    执行方式 / 完整语句列表）与命名变量。旧文本任务没有任务组名称/执行方式时
    不编造；表单任务的名称/执行方式来自 groups_json 原始分组。
    """

    def _context(run_id):
        run = _fetch_run(run_id)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)

        try:
            groups = json.loads(run.get("groups_json") or "[]")
        except (ValueError, TypeError):
            raise _ToolError("DATA_INVALID", "任务的 groups_json 无法解析", False)
        try:
            variables = json.loads(run.get("variables_json") or "{}")
        except (ValueError, TypeError):
            raise _ToolError("DATA_INVALID", "任务的 variables_json 无法解析", False)
        if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
            raise _ToolError("DATA_INVALID", "groups_json 必须为对象列表", False)
        if not isinstance(variables, dict):
            raise _ToolError("DATA_INVALID", "variables_json 必须为对象", False)
        try:
            tasks = resolve_tasks(run)
        except Exception as e:  # 解析失败是数据问题，不是"任务不存在"
            raise _ToolError(
                "DATA_INVALID",
                _redact(f"任务组解析失败：{type(e).__name__}: {e}"),
                False,
            ) from e

        # 表单分组才有 name / execution_mode；task 本身只有 sql_id/weight/statements。
        # 配对规则与 run_form.group_tasks 一致：空 SQL 的分组不产生 task，
        # 其余分组按顺序对应 tasks[0..n]（sql_id 序号即此顺序）。
        warnings: list = []
        meta_by_sql_id: dict = {}
        non_empty = [g for g in groups if isinstance(g, dict) and str(g.get("sql", "")).strip()]
        if non_empty and len(non_empty) == len(tasks):
            for task, g in zip(tasks, non_empty):
                meta_by_sql_id[task["sql_id"]] = {
                    "group_id": g.get("id"),
                    "name": g.get("name"),
                    "execution_mode": g.get("execution_mode"),
                }
        elif groups:
            warnings.append("任务组数量与解析出的任务不一致，跳过任务组元数据配对")

        data = {
            "run_id": run["id"],
            "name": run["name"],
            "status": run["status"],
            "config": {
                "concurrency": run["concurrency"],
                "spawn_rate": run["spawn_rate"],
                "duration_sec": run["duration_sec"],
            },
            "times": {
                "created_at": run.get("created_at"),
                "started_at": run.get("started_at"),
                "ended_at": run.get("ended_at"),
            },
            "sql_source": run["sql_source"],
            # 原始完整 SQL：文本模式为用户提交原文；表单模式为 prepare() 生成的
            # 等价重构文本（权威分组见 groups）。
            "sql_content": run["sql_content"],
            "groups": groups,
            "variables": variables,
            "tasks": [],
            "error_msg": _redact(run.get("error_msg")),
        }
        for t in tasks or []:
            entry = {
                "sql_id": t.get("sql_id"),
                "weight": t.get("weight"),
                "statements": list(t.get("statements") or []),
            }
            meta = meta_by_sql_id.get(entry["sql_id"])
            if meta:  # 旧任务没有这些字段，不编造
                entry["group_id"] = meta["group_id"]
                entry["name"] = meta["name"]
                entry["execution_mode"] = meta["execution_mode"]
            data["tasks"].append(entry)
        if run["status"] in ("pending", "running"):
            warnings.append(f"任务状态为 {run['status']}，压测尚未结束，以上是提交时的配置与 SQL")
        return _ok(run_id, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(
            None,
            "INVALID_RUN_ID",
            "run_id 必须是 8 位十六进制（如 bb01ea65）",
            False,
        )
    return _wrap(normalized, _context)


# ---------------------------------------------------------------------------
# 工具 2：get_run_metrics
# ---------------------------------------------------------------------------

# 使用报告模块的只读 CSV 读取函数，不调用 build_l0/generate_report。
# 只做字段映射；不在 AI 层重写时序聚合或 L1 规则。
_STATS_FIELDS = {
    "total_requests": "Request Count", "total_failures": "Failure Count",
    "avg_ms": "Average Response Time", "p50_ms": "50%",
    "p95_ms": "95%", "p99_ms": "99%", "max_ms": "Max Response Time",
    "qps": "Requests/s",
}


def _stats_fields(row):
    result = {}
    for key, column in _STATS_FIELDS.items():
        raw = row.get(column)
        try:
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                value = None
            if key in ("total_requests", "total_failures") and value is not None:
                value = int(value) if value.is_integer() else None
        except (ValueError, TypeError, OverflowError):
            value = None
        result[key] = value
    total, failures = result["total_requests"], result["total_failures"]
    result["err_rate"] = failures / total if total and failures is not None and failures <= total else None
    return result


# --- 时序证据：逐秒采样（metrics 表）的降采样与分段 ---------------------------
# 采集侧（collector）在压测期间每秒写入一行；这里只在读取时聚合，不改采集与存储。
# 聚合口径按字段性质区分（级别 mean / 尖峰敏感 max / 增量 sum），禁止混用。

_SERIES_MAX_POINTS = 60
# (字段, 聚合方式, 小数位)
_SERIES_COLUMNS = (
    ("qps", "mean", 1),
    ("p95_ms", "max", 1),
    ("p99_ms", "max", 1),
    ("err_rate", "mean", 4),
    ("threads_running", "mean", 0),
    ("threads_connected", "max", 0),
    ("mysql_qps", "mean", 1),
    ("slow_inc", "sum", 0),
    ("lock_waits_inc", "sum", 0),
    ("tmp_disk_inc", "sum", 0),
    ("bufpool_hit", "mean", 3),
)

# 分段：爬坡段（并发/爬坡速率）上限占比、尾段占比、稳定段最低占比
_SEGMENT_RAMP_MAX_SHARE = 0.15
_SEGMENT_TAIL_SHARE = 0.10
_SEGMENT_MIN_STEADY_SHARE = 0.20
# (源字段, 输出字段名, 聚合方式, 小数位)：输出名带口径后缀，避免被当成同一种值
_SEGMENT_FIELDS = (
    ("qps", "qps_mean", "mean", 1),
    ("p95_ms", "p95_max", "max", 1),
    ("p99_ms", "p99_max", "max", 1),
    ("err_rate", "err_rate_mean", "mean", 4),
    ("threads_running", "threads_running_mean", "mean", 0),
    ("threads_connected", "threads_connected_max", "max", 0),
    ("slow_inc", "slow_inc_sum", "sum", 0),
    ("lock_waits_inc", "lock_waits_inc_sum", "sum", 0),
    ("tmp_disk_inc", "tmp_disk_inc_sum", "sum", 0),
    ("bufpool_hit", "bufpool_hit_mean", "mean", 3),
)


def _epoch_seconds(iso_text) -> Optional[float]:
    """started_at（本地时间 ISO 字符串）→ epoch 秒；解析失败返回 None。"""
    if not isinstance(iso_text, str) or not iso_text:
        return None
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except ValueError:
        return None


def _rounded(value: float, digits: int):
    return int(round(value)) if digits == 0 else round(value, digits)


def _aggregate(values, how: str, digits: int):
    numbers = [v for v in values if v is not None]
    if not numbers:
        return None
    if how == "sum":
        result = sum(numbers)
    elif how == "max":
        result = max(numbers)
    else:
        result = sum(numbers) / len(numbers)
    return _rounded(result, digits)


def _point_value(point: dict, key: str) -> Optional[float]:
    value = point.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _series_payload(points: list, started_at) -> Optional[dict]:
    """原始采样点 → 降采样序列（≤ _SERIES_MAX_POINTS 点，列式数组）。

    >60 点时按 ts 时间等分桶（采样间隔不保证 1 秒，不能按索引等分）；
    空桶输出 null 并计入 gap_buckets；整列全空时删除该列并列入 missing_columns。
    """
    if not points:
        return None
    t0 = _epoch_seconds(started_at)
    used_fallback_t0 = t0 is None
    if t0 is None:
        t0 = float(points[0]["ts"])  # 兜底：以首个采样点为原点
    offsets = [max(0.0, float(p["ts"]) - t0) for p in points]
    samples = len(points)
    downsampled = samples > _SERIES_MAX_POINTS
    bucket_sec = None
    if downsampled:
        base = offsets[0]
        span = offsets[-1] - base
        if span > 0:
            bucket_sec = span / (_SERIES_MAX_POINTS - 1)
            bucket_count = int(span // bucket_sec) + 1
            groups: list = [[] for _ in range(bucket_count)]
            for offset, point in zip(offsets, points):
                groups[int((offset - base) // bucket_sec)].append(point)
            out_offsets = [round(base + bucket_sec * i, 1) for i in range(bucket_count)]
        else:
            downsampled = False  # 所有点同一时刻（异常数据）：退化为原样
    if not downsampled:
        groups = [[point] for point in points]
        out_offsets = [round(offset, 1) for offset in offsets]

    columns = {}
    missing = []
    for key, how, digits in _SERIES_COLUMNS:
        values = [_aggregate([_point_value(p, key) for p in group], how, digits)
                  for group in groups]
        if all(v is None for v in values):
            missing.append(key)
        else:
            columns[key] = values

    gaps = sum(1 for group in groups if not group) if downsampled else 0
    deltas = [b - a for a, b in zip(offsets, offsets[1:])]
    payload = {
        "t0": started_at if not used_fallback_t0 else None,
        "samples": samples,
        "points": len(groups),
        "downsampled": downsampled,
        "bucket_sec": round(bucket_sec, 2) if bucket_sec else None,
        "gap_buckets": gaps,
        "coverage": round(1 - gaps / len(groups), 3) if downsampled and groups else None,
        "max_gap_sec": int(round(max(deltas))) if deltas else 0,
        "aggregation": {key: how for key, how, _ in _SERIES_COLUMNS},
        "missing_columns": missing,
        "offsets_sec": out_offsets,
        **columns,
    }
    return payload


def _segments_payload(points: list, run: dict, started_at) -> tuple:
    """按爬坡/稳定/尾段切分（用实际数据窗口）。返回 (segments|None, note|None)。

    只给事实（各段聚合值），"哪段劣化"由模型自己比较得出。
    """
    if len(points) < 3:
        return None, "采样点过少（<3），未做分段"
    t0 = _epoch_seconds(started_at)
    if t0 is None:
        t0 = float(points[0]["ts"])
    offsets = [max(0.0, float(p["ts"]) - t0) for p in points]
    total = offsets[-1]
    if total <= 0:
        return None, "采样窗口过短，未做分段"
    try:
        ramp = min(math.ceil(run["concurrency"] / run["spawn_rate"]),
                   total * _SEGMENT_RAMP_MAX_SHARE)
    except (TypeError, ZeroDivisionError, KeyError):
        ramp = 0.0
    tail = total * _SEGMENT_TAIL_SHARE
    steady = total - ramp - tail
    if steady < total * _SEGMENT_MIN_STEADY_SHARE:
        return None, f"任务时长约 {total:.0f}s 过短（或爬坡占比过大），未做分段"

    edges = [0.0]
    names = []
    if ramp >= 1.0:
        edges.append(ramp)
        names.append("ramp")
    edges += [total - tail, total]
    names += ["steady", "tail"]

    segments = []
    for index, name in enumerate(names):
        lo, hi = edges[index], edges[index + 1]
        last = index == len(names) - 1
        subset = [p for p, o in zip(points, offsets)
                  if (lo <= o <= hi if last else lo <= o < hi)]
        entry = {"name": name,
                 "window_sec": [round(lo, 1), round(hi, 1)],
                 "samples": len(subset)}
        if subset:
            for key, out_key, how, digits in _SEGMENT_FIELDS:
                value = _aggregate([_point_value(p, key) for p in subset], how, digits)
                if value is not None:
                    entry[out_key] = value
        segments.append(entry)
    return segments, None


def get_run_metrics(run_id: str) -> dict:
    """读取累计 stats.csv、逐秒采样与已有报告；不生成报告、不重新采集。

    stats.csv 提供请求统计；metrics 表提供逐秒时序（降采样为 series、分窗口为
    segments）；报告提供已计算的采样摘要及 L1。
    CSV 缺失时不将旧报告中可能由缺失默认生成的零计数当作证据。
    """
    def _metrics(run_id):
        run = _fetch_run(run_id)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)
        warnings = []
        with _readonly_conn() as conn:
            row = conn.execute(
                "SELECT l0_json, l1_json, created_at FROM reports WHERE run_id=?",
                (run_id,),
            ).fetchone()
            metric_rows = conn.execute(
                "SELECT ts, qps, p95_ms, p99_ms, err_rate, threads_running,"
                " threads_connected, mysql_qps, slow_inc, lock_waits_inc,"
                " tmp_disk_inc, bufpool_hit"
                " FROM metrics WHERE run_id=? ORDER BY ts",
                (run_id,),
            ).fetchall()
        l0, l1, report_time = {}, None, None
        if row:
            try:
                parsed_l0, parsed_l1 = json.loads(row["l0_json"]), json.loads(row["l1_json"])
                if not isinstance(parsed_l0, dict) or not isinstance(parsed_l1, list):
                    raise ValueError("报告结构不正确")
                if any(not isinstance(item, dict) for item in parsed_l1):
                    raise ValueError("L1 应为对象列表")
                l0, l1, report_time = parsed_l0, parsed_l1, row["created_at"]
            except (ValueError, TypeError):
                warnings.append("已保存报告损坏，忽略报告，仍尝试读取 stats.csv")
        aggregate, per_rows = read_stats(run_id)
        summary = _stats_fields(aggregate or {})
        sources = {key: "locust_stats_csv" if value is not None else None
                   for key, value in summary.items()}
        # 复用报告已经生成的采样汇总；不另算一套值。
        for key in ("qps_avg", "qps_peak", "threads_running_p95",
                    "mysql_slow_total", "mysql_lock_waits_total"):
            value = l0.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                value = None
            summary[key] = value
            sources[key] = "saved_report.l0" if value is not None else None
        per_sql = {}
        for item in per_rows:
            sql_id = item.get("Name") or ""
            if re.fullmatch(r"sql_\d+", sql_id):
                per_sql[sql_id] = _stats_fields(item)
        if aggregate is None:
            warnings.append("缺少 stats.csv 汇总行；累计请求统计保持 null，不用历史窗口统计替代")
        if not row:
            warnings.append("尚无已保存报告；采样汇总和 L1 暂不可用")
        in_progress = run["status"] in ("pending", "running")
        if in_progress:
            warnings.append("运行中数据是读取时快照，CSV、时序与报告可能来自不同时间")
        warnings.append("已有报告的增量合计可能把真实零写成 null；本工具保留该缺失状态")

        points = [dict(r) for r in metric_rows]
        series = _series_payload(points, run.get("started_at"))
        segments, segment_note = _segments_payload(points, run, run.get("started_at"))
        if not points:
            warnings.append("无逐秒采样（metrics 为空：采集未运行、平台重启或历史任务），"
                            "series 与 segments 不可用，仅累计统计可参考")
        if segment_note:
            warnings.append(segment_note)

        return _ok(run_id, {
            "run_id": run_id, "status": run["status"], "in_progress": in_progress,
            "data_available": aggregate is not None or bool(per_sql) or bool(l0) or bool(points),
            "summary": summary, "summary_sources": sources,
            "per_sql": per_sql, "per_sql_source": "locust_stats_csv",
            "series": series, "segments": segments,
            "l1_findings": l1, "report_created_at": report_time,
            "missing": [key for key, value in summary.items() if value is None],
            "field_notes": {
                "requests": "一次 Locust 请求对应整个任务组，不是单条 SQL；延迟单位 ms",
                "err_rate": "累计失败数/累计请求数，范围 0 到 1；零请求时为 null",
                "qps": "stats.csv 的 Requests/s；不能与报告的采样 QPS 均值混为同一口径",
                "qps_avg": "已有报告中的采样请求速率算术平均",
                "mysql": "实例级增量，不能直接归因于本次压测或某条 SQL",
                "l1_findings": "已有规则结果，仅作为诊断输入，不等同确定根因",
                "series": "逐秒采样的降采样序列（列式数组，最多 60 点）；各字段聚合口径见 "
                          "aggregation：mean=窗口均值、max=窗口内最坏值、sum=窗口内总量，"
                          "三者的含义不同，不可混用；p95_ms/p99_ms 是窗口最大值，不是分位数平均",
                "series_offsets": "offsets_sec 相对 started_at（t0），可与 failures 的 "
                                  "offset 字段直接对齐；coverage<1 或 gap_buckets>0 表示采样有空洞",
                "segments": "按爬坡/稳定/尾段切分的窗口摘要，字段名自带口径后缀（_mean/_max/_sum）；"
                            "未分段时 segments 为 null 并给出说明。哪段劣化需要模型自行比较",
            },
        }, warnings)
    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(None, "INVALID_RUN_ID", "run_id 必须是 8 位十六进制", False)
    return _wrap(normalized, _metrics)


# ---------------------------------------------------------------------------
# 工具 3：get_run_failures
# ---------------------------------------------------------------------------

_FAILURE_MAX_GROUPS = 10
_FAILURE_MAX_EXCEPTIONS = 5
_FAILURE_MSG_CHARS = 300


def _short_text(value, limit: int = _FAILURE_MSG_CHARS) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_seen(value) -> Optional[float]:
    """locust 的 First/Last Seen 兼容两种真实格式：epoch 秒（浮点）与 ISO 8601（带 Z）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_csv_rows(path) -> Optional[list]:
    """读 CSV 为 dict 列表；文件不存在/不可读返回 None（与"仅表头的空表"区分）。"""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return None


def get_run_failures(run_id: str) -> dict:
    """读取 locust 失败明细（failures.csv / exceptions.csv）：错误类型、次数、首末时间。

    只读已有产物，不连接目标库。零失败的任务也会留下只有表头的文件；
    文件缺失按 FAILURES_NOT_AVAILABLE 返回（可重试），与"零失败"是两种状态。
    """

    def _failures(run_id):
        run = _fetch_run(run_id)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)
        base = settings.data_dir / "locust"
        failures_path = base / f"{run_id}_failures.csv"
        exceptions_path = base / f"{run_id}_exceptions.csv"
        rows = _read_csv_rows(failures_path)
        if rows is None:
            reason = ("任务仍在运行，失败明细尚未生成"
                      if run["status"] in ("pending", "running")
                      else "产物缺失（可能平台重启或该任务未生成）")
            raise _ToolError(
                "FAILURES_NOT_AVAILABLE",
                f"未找到 {failures_path.name}：{reason}；这不代表没有失败，"
                "请以 stats.csv 的失败数（summary.total_failures）为准",
                True,
            )

        t0 = _epoch_seconds(run.get("started_at"))
        warnings: list = []
        grouped: dict = {}
        total = 0
        for row in rows:
            name = (row.get("Name") or "").strip()
            message = _short_text(row.get("Error"))
            try:
                count = int(float(row.get("Occurrences") or 0))
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue
            entry = grouped.setdefault((name, message),
                                       {"occurrences": 0, "first": None, "last": None})
            entry["occurrences"] += count
            total += count
            for field, key in (("First Seen", "first"), ("Last Seen", "last")):
                seen = _parse_seen(row.get(field))
                if seen is None:
                    continue
                current = entry[key]
                entry[key] = seen if current is None else (
                    min(current, seen) if key == "first" else max(current, seen))

        groups = []
        for (name, message), entry in sorted(grouped.items(),
                                             key=lambda item: -item[1]["occurrences"]):
            group = {
                "sql_id": name or None,
                "error": message,
                "occurrences": entry["occurrences"],
                "share": round(entry["occurrences"] / total, 4) if total else None,
            }
            if t0 is not None:
                for out_key, key in (("first_offset_sec", "first"), ("last_offset_sec", "last")):
                    seen = entry[key]
                    group[out_key] = int(max(0.0, seen - t0)) if seen is not None else None
            groups.append(group)
        omitted = max(0, len(groups) - _FAILURE_MAX_GROUPS)
        groups = groups[:_FAILURE_MAX_GROUPS]

        exceptions = []
        for row in _read_csv_rows(exceptions_path) or []:
            try:
                count = int(float(row.get("Count") or 0))
            except (TypeError, ValueError):
                count = 0
            message = _short_text(row.get("Message"))
            if count > 0 and message:
                exceptions.append({"message": message, "count": count})
        exceptions.sort(key=lambda item: -item["count"])
        exceptions = exceptions[:_FAILURE_MAX_EXCEPTIONS]

        if omitted:
            warnings.append(f"失败分组超过 {_FAILURE_MAX_GROUPS} 组，按出现次数保留前 "
                            f"{_FAILURE_MAX_GROUPS} 组，省略 {omitted} 组")
        if not groups:
            warnings.append("failures.csv 存在但无失败记录：本次任务零失败")
        if t0 is None:
            warnings.append("缺少 started_at，offset 字段未给出")
        # 顺手做一次与 stats.csv 的一致性检查（口径不一致时给警告，不擅自改数）
        aggregate, _ = read_stats(run_id)
        if aggregate is not None:
            try:
                stats_failures = int(float(aggregate.get("Failure Count") or 0))
            except (TypeError, ValueError):
                stats_failures = None
            if stats_failures is not None and stats_failures != total:
                warnings.append(f"failures.csv 合计 {total} 与 stats.csv 失败数 "
                                f"{stats_failures} 不一致，口径可能有差异")

        data = {
            "run_id": run_id,
            "available": True,
            "source": failures_path.name,
            "t0": run.get("started_at"),
            "total_failures": total,
            "group_count": len(groups) + omitted,
            "groups": groups,
            "exceptions": exceptions,
            "field_notes": {
                "occurrences": "该错误类型的累计出现次数；与 stats.csv 的 Failure Count 同源",
                "share": "占全部失败的比例（0 到 1）",
                "offsets": "first/last_offset_sec 相对 started_at，可与 metrics series 的 offsets_sec 对齐",
                "exceptions": "取自 exceptions.csv，仅 Message（已清洗并截断），不含 Traceback",
                "empty": "groups 为空表示零失败；工具报错（FAILURES_NOT_AVAILABLE）才是明细缺失",
            },
        }
        return _ok(run_id, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(None, "INVALID_RUN_ID", "run_id 必须是 8 位十六进制", False)
    return _wrap(normalized, _failures)


# ---------------------------------------------------------------------------
# 工具 4：get_run_explain
# ---------------------------------------------------------------------------

_CAVEATS_EXPLAIN = [
    "EXPLAIN 的 rows 是优化器估算值，不是真实扫描行数。",
    "findings 是基于计划形态的启发式解释，不是确定根因；本工具原样保留计划与 findings，"
    "不把解释升级成根因。",
    "filled/parameters 是采集时按 run_id 种子重新生成的样本参数，不是压测中某次请求的"
    "参数回放（压测不回传请求内容）。",
    f"采集发生在压测结束后（phase={PHASE_POST_RUN}）；含写入的压测会让表的统计信息在"
    "之后变化，计划可能已与压测时不同。",
    "一个 sql_id（任务组）含多条语句，statements[] 是双层结构，不能假设一对一。",
]


def get_run_explain(run_id: str) -> dict:
    """回答「已有采样执行计划提供了什么证据」。

    只读已有产物 ``data/explain/{run_id}.json``：不连接目标 MySQL、不重新执行
    EXPLAIN。区分六种状态：任务不存在 / 仍在运行尚无产物 / 已结束但暂无产物 /
    产物存在但采集失败 / 部分语句采集成功 / 产物损坏。已结束但暂无产物不直接
    判定为永久失败（采集开关、平台重启等都可能导致）。语句级成功与否不只依赖
    产物顶层 ok，逐条检查 findings 计算 statement_status。
    """

    def _explain(run_id):
        run = _fetch_run(run_id)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)

        if run["status"] in ("pending", "running"):
            raise _ToolError(
                "EXPLAIN_NOT_READY",
                f"任务状态为 {run['status']}；EXPLAIN 在压测收尾时才采集，尚无产物",
                True,
            )

        path = artifact_path(run_id)
        if not path.is_file():
            message = (
                "任务已结束，但没有 EXPLAIN 采集产物。可能原因：采集开关关闭、平台重启"
                "中断（收尾采集只对本进程启动的压测有效）、或采集时出错。这不代表压测失败，"
                "也不能据此判断采集是永久失败。"
            )
            if not settings.explain_probe_enabled:
                message += " 当前配置 EXPLAIN_PROBE_ENABLED=false。"
            raise _ToolError("EXPLAIN_NOT_AVAILABLE", message + " 也可能仍在收尾采集中；允许上层有限重试。", True)

        artifact = load_artifact(run_id)
        if not isinstance(artifact, dict):
            raise _ToolError(
                "EXPLAIN_CORRUPT",
                f"产物存在但无法解析（JSON 损坏或结构不是对象）：{path}",
                False,
            )
        if artifact.get("run_id") != run_id:
            raise _ToolError(
                "EXPLAIN_CORRUPT",
                f"产物记录的 run_id（{artifact.get('run_id')!r}）与请求的 {run_id} 不一致",
                False,
            )
        tasks = artifact.get("tasks")
        if not isinstance(tasks, list):
            raise _ToolError("EXPLAIN_CORRUPT", "产物缺少 tasks 列表（结构不完整）", False)

        if not isinstance(artifact.get("summary", {}), dict):
            raise _ToolError("EXPLAIN_CORRUPT", "summary 必须是对象", False)
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get("statements"), list):
                raise _ToolError("EXPLAIN_CORRUPT", "任务必须包含 statements 列表", False)
            for statement in task["statements"]:
                if not isinstance(statement, dict):
                    raise _ToolError("EXPLAIN_CORRUPT", "语句必须是对象", False)
                findings = statement.get("findings") or []
                if not isinstance(findings, list) or any(not isinstance(f, dict) for f in findings):
                    raise _ToolError("EXPLAIN_CORRUPT", "findings 必须是对象列表", False)
                if statement.get("ok") is True and not isinstance(statement.get("plan"), list):
                    raise _ToolError("EXPLAIN_CORRUPT", "成功语句必须包含 plan 列表", False)

        # 仅统计采集事实，不重新解释执行计划。
        statement_status = {
            "total": 0, "probed_ok": 0, "skipped_not_explainable": 0,
            "compile_error": 0, "explain_error": 0, "not_probed": 0, "other_failed": 0,
        }
        for t in tasks:
            if not isinstance(t, dict):
                continue
            for st in t.get("statements") or []:
                if not isinstance(st, dict):
                    continue
                statement_status["total"] += 1
                if st.get("ok") is True:
                    statement_status["probed_ok"] += 1
                    continue
                codes = {
                    f.get("code") for f in st.get("findings") or [] if isinstance(f, dict)
                }
                hit = False
                for key in ("skipped_not_explainable", "compile_error", "explain_error", "not_probed"):
                    if key in codes:
                        statement_status[key] += 1
                        hit = True
                        break
                if not hit:
                    statement_status["other_failed"] += 1

        collected = statement_status["probed_ok"] > 0
        summ = artifact.get("summary") or {}
        warnings: list = []
        if artifact.get("ok") is not True:
            warnings.append("产物顶层 ok 非 true：采集流程报告失败，"
                            "error 字段有原因；计划证据可能为空")
        if summ.get("errors"):
            warnings.append(f"summary 记录 errors={summ['errors']}：部分语句采集失败")
        if statement_status["total"] and statement_status["probed_ok"] < statement_status["total"]:
            warnings.append(
                f"{statement_status['total']} 条语句中 {statement_status['probed_ok']} 条有执行计划，"
                "其余被跳过/失败/达采集上限"
            )
        if artifact.get("truncated"):
            warnings.append(f"采集被截断：{artifact.get('truncated_reason')}")
        if not statement_status["total"]:
            warnings.append("产物没有任何语句条目")

        target = artifact.get("target")
        data = {
            "run_id": artifact.get("run_id"),
            "present": True,
            "collected": collected,
            "probe_ok": artifact.get("ok") is True,
            "collection_status": (
                "complete" if collected and statement_status["probed_ok"] == statement_status["total"]
                and not artifact.get("truncated") else "partial" if collected else "no_plans"
            ),
            "probe_version": artifact.get("probe_version"),
            "phase": artifact.get("phase"),
            "generated_at": artifact.get("generated_at"),
            "server_version": artifact.get("server_version"),
            "probe_error": _redact(artifact.get("error")),
            # target 按白名单取，即使契约变化也不外发多余字段（契约本身不含 password）
            "target": {k: target.get(k) for k in ("host", "port", "database", "user")}
            if isinstance(target, dict) else None,
            "truncated": artifact.get("truncated"),
            "truncated_reason": artifact.get("truncated_reason"),
            "summary": summ,
            "statement_status": statement_status,
            "tasks": tasks,
            "notes": artifact.get("notes") if isinstance(artifact.get("notes"), list) else None,
            "caveats": list(_CAVEATS_EXPLAIN),
        }
        return _ok(run_id, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(
            None,
            "INVALID_RUN_ID",
            "run_id 必须是 8 位十六进制（如 bb01ea65）",
            False,
        )
    return _wrap(normalized, _explain)


# ---------------------------------------------------------------------------
# 工具 5：get_table_profile（本模块唯一连目标库的工具，需显式开关）
# ---------------------------------------------------------------------------

def get_table_profile(run_id: str, table_name: str) -> dict:
    """读取目标库中一张表的列定义、全部索引与表规模估算（只读）。

    连接来自 run 快照（``runs.db_dsn_json``），**不接受调用方指定 host/database**；
    表名先过白名单校验。需显式开启 ``AI_DB_PROBE_ENABLED``，否则直接返回
    ``DB_PROBE_DISABLED`` 且不连库。只查 information_schema，不读业务数据行、
    不 COUNT/DISTINCT、不建索引。读到的元数据是**此刻**的库状态，不是压测时的快照。
    """

    def _profile(run_id):
        if not db_profile.enabled():
            raise _ToolError(
                "DB_PROBE_DISABLED",
                "未开启目标库元数据读取（AI_DB_PROBE_ENABLED=false）；开启后才会连库",
                False,
            )
        try:
            table = db_profile.validate_table_name(table_name)
        except db_profile.DbProfileError as e:
            raise _ToolError(e.code, e.message, e.retryable) from e

        run = _fetch_run(run_id)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)
        dsn = _fetch_run_dsn(run_id)
        if dsn is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)

        try:
            profile = db_profile.read_table_profile(dsn, table)
        except db_profile.DbProfileError as e:
            raise _ToolError(e.code, _redact(e.message), e.retryable) from e

        warnings: list = []
        if run["status"] in ("pending", "running"):
            warnings.append("任务状态为 "
                            f"{run['status']}，库状态仍在变化；这里读到的只是此刻的元数据")
        data = {
            "run_id": run_id,
            "run_status": run["status"],
            # 只给白名单字段；dsn 里的 password 绝不外发
            "target": {k: dsn.get(k) for k in ("host", "port", "database", "user")},
        }
        data.update(profile)
        return _ok(run_id, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(
            None,
            "INVALID_RUN_ID",
            "run_id 必须是 8 位十六进制（如 bb01ea65）",
            False,
        )
    return _wrap(normalized, _profile)
