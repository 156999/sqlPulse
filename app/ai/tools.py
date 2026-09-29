"""SQLPulse 只读诊断工具。

三个工具分别回答三个问题：
- ``get_run_context(run_id)``：这次压测测了什么
- ``get_run_metrics(run_id)``：这次压测表现如何
- ``get_run_explain(run_id)`` ：已有采样执行计划提供了什么证据

设计约束：

- **纯只读**：不连接目标 MySQL、不执行 EXPLAIN、不生成报告、不初始化/迁移
  SQLite、不恢复任务状态、不写任何数据文件；不 import ``app.main``。
- **自身无写操作**：不创建目录/文件、不初始化 SQLite。注意：导入 ``app.config``
  会建目录、且 AUTH_ENABLED=true 时要求 APP_SECRET_KEY（config.py 现状）；
  验证入口在导入前自备环境（设 DATA_DIR、按需关鉴权）。
- SQLite 走独立只读连接（``file:...?mode=ro``），库文件不存在时报
  ``DATA_UNAVAILABLE``、不自动建库（``db.get_conn()`` 缺库时会建库，不复用）。
- runs 行按字段白名单读取（不含 ``db_dsn_json``：连接 JSON 含密码）。
- 统一外层结构 ``{ok, run_id, data, warnings, error}``，错误对象
  ``{code, message, retryable}``；返回全部 JSON 可序列化。
- 未知异常包装为 ``TOOL_INTERNAL_ERROR``，不伪装成「任务不存在」。
"""

import csv as csvmod
import json
import re
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings
from app.services.explain_probe import PHASE_POST_RUN, artifact_path, load_artifact
from app.services.runner import resolve_tasks

# ---------------------------------------------------------------------------
# 公共：run_id 校验与只读访问
# ---------------------------------------------------------------------------

# 仓库真实 ID 规则：routers/tasks.py 用 uuid4().hex[:8]（8 位十六进制）。
# 校验同时挡住路径穿越（run_id 会进入 explain 产物文件名）。
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# runs 表字段白名单（不含 db_dsn_json：它含目标库密码）
_RUN_COLS = (
    "id", "name", "status", "sql_source", "sql_content",
    "variables_json", "groups_json",
    "concurrency", "spawn_rate", "duration_sec",
    "error_msg", "created_at", "started_at", "ended_at",
)

# 错误文本里常见的凭据形态，兜底清洗（error_msg 等可能夹带密码）
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
        "data": data,
        "warnings": list(warnings or []),
        "error": None,
    }


def _fail(run_id: str, code: str, message: str, retryable: bool) -> dict:
    return {
        "ok": False,
        "run_id": run_id,
        "data": None,
        "warnings": [],
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _redact(text) -> str:
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
        return fn()
    except _ToolError as e:
        return _fail(run_id, e.code, e.message, e.retryable)
    except Exception as e:  # noqa: BLE001 工具边界：任何意外都不能伪装成"任务不存在"
        return _fail(
            run_id,
            "TOOL_INTERNAL_ERROR",
            _redact(f"工具内部错误：{type(e).__name__}: {e}"),
            False,
        )


def _iso_seconds(started: Optional[str], ended: Optional[str]) -> Optional[int]:
    if not started or not ended:
        return None
    try:
        return max(
            0, int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())
        )
    except ValueError:
        return None


def _seconds_since(iso: Optional[str]) -> Optional[int]:
    """距今秒数（用于判断「刚结束、收尾可能仍在进行」）；解析不了返回 None。"""
    if not iso:
        return None
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(iso)).total_seconds()))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 工具侧严格 CSV 解析：不复用 collector 的版本——它缺列填 0、fails 是
# Failures/s 速率；工具要求缺失=null、真实零保留、速率不冒充次数，
# 且不改动页面口径，所以独立实现。
# ---------------------------------------------------------------------------

_SQL_NAME_RE = re.compile(r"sql_\d+")


def _csv_num(row: dict, keys: list) -> Optional[float]:
    """严格取数：列缺失/空串/N/A/非数值 → None（未采集）；真实 0 保留为 0。"""
    for k in keys:
        if k not in row:
            continue
        v = row.get(k)
        if v in (None, "", "N/A"):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def _read_last_history_strict(csv_path: Path) -> Optional[dict]:
    """读 *_stats_history.csv 最后一行 Aggregated：缺列不填 0；
    err_rate 只在两个计数可核实且总请求 > 0 时计算，否则 None。
    """
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = [r for r in csvmod.DictReader(f) if r.get("Name") == "Aggregated"]
    except OSError:
        return None
    if not rows:
        return None
    r = rows[-1]
    total = _csv_num(r, ["Total Request Count", "Request Count"])
    fail = _csv_num(r, ["Total Failure Count", "Failure Count"])
    err_rate = None
    if total is not None and total > 0 and fail is not None:
        err_rate = fail / total
    # total == 0 时 err_rate 无定义 → None（区别于"未采集"，见 field_notes）
    return {
        "qps": _csv_num(r, ["Requests/s", "Current RPS"]),
        "avg_ms": _csv_num(r, ["Total Average Response Time", "Average Response Time"]),
        "p95_ms": _csv_num(r, ["95%"]),
        "p99_ms": _csv_num(r, ["99%"]),
        "err_rate": err_rate,
        "total_requests": total,
        "total_failures": fail,
    }


def _read_per_sql_history_strict(csv_path: Path) -> dict:
    """按 sql_id 返回时序 [{ts, rps, failures_per_sec, avg_ms, p95_ms, p99_ms}]。
    failures_per_sec 即 Failures/s（速率不是次数）；缺失值一律 None。
    """
    out: dict = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csvmod.DictReader(f):
                name = r.get("Name", "")
                if not _SQL_NAME_RE.fullmatch(name):
                    continue  # 跳过 Aggregated / 其它条目
                ts = _csv_num(r, ["Timestamp"])
                if ts is None:
                    continue
                out.setdefault(name, []).append({
                    "ts": int(ts),
                    "rps": _csv_num(r, ["Requests/s", "Current RPS"]),
                    "failures_per_sec": _csv_num(r, ["Failures/s"]),
                    "avg_ms": _csv_num(r, ["Total Average Response Time", "Average Response Time"]),
                    "p95_ms": _csv_num(r, ["95%"]),
                    "p99_ms": _csv_num(r, ["99%"]),
                })
    except OSError:
        return {}
    return out


# ---------------------------------------------------------------------------
# 工具 1：get_run_context
# ---------------------------------------------------------------------------

def get_run_context(run_id: str) -> dict:
    """回答「这次压测测了什么」。run_id 自动归一化（8 位十六进制）。

    返回任务参数、时间、SQL 来源、原始完整 SQL、任务组（SQL ID / 名称 / 权重 /
    执行方式 / 完整语句列表）与命名变量。旧文本任务没有组名/执行方式时不编造；
    表单任务的组名/执行方式来自 groups_json。
    """

    def _context():
        run = _fetch_run(normalized)
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
        try:
            tasks = resolve_tasks(run)
        except Exception as e:  # 解析失败是数据问题，不是"任务不存在"
            raise _ToolError(
                "DATA_INVALID",
                _redact(f"任务组解析失败：{type(e).__name__}: {e}"),
                False,
            ) from e

        # name/execution_mode 只在表单分组里有；配对规则与 run_form.group_tasks
        # 一致（空 SQL 分组不产生 task，sql_id 序号即顺序）。
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
            # 原始完整 SQL：文本模式为提交原文；表单模式为 prepare() 重构文本（权威见 groups）。
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
        return _ok(normalized, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(
            _redact(str(run_id)),
            "INVALID_RUN_ID",
            "run_id 必须是 8 位十六进制（如 bb01ea65）",
            False,
        )
    return _wrap(normalized, _context)


# ---------------------------------------------------------------------------
# 工具 2：get_run_metrics
# ---------------------------------------------------------------------------

_CAVEATS_METRICS = [
    "Locust 一次请求对应一个 SQL 任务组（statements 列表）的完整执行，"
    "QPS/RPS 是任务组请求速率，不是单条 SQL 的 QPS。",
    "mysql_* 指标来自 SHOW GLOBAL STATUS，是实例级数据，受同实例其他任务/业务影响，"
    "不能归因到单条 SQL。",
    "mysql_qps / slow_inc / lock_waits_inc / tmp_disk_inc 是相邻采样的计数差分，"
    "采样间隔约 1 秒但未按实际时间归一化，不是经过校准的每秒速率；"
    "对差分增量求和得到的是采样窗口内的次数合计。",
    "err_rate 为累计口径；历史 CSV 中的 p95/p99 是 Locust 的窗口分位（窗口大小取决于 "
    "Locust 版本），avg_ms 优先为累计均值，不要把每个采样点都解释成「这一秒内的测量」，"
    "也不要把多个窗口 P99 平均成全程 P99。",
    "per_sql.series 的 failure_rate_per_sec_* 来自 Failures/s，是失败请求速率；"
    "本工具不把速率求和当累计失败次数。已保存报告里的累计失败数在 stats.csv 缺列时"
    "会被报告侧填 0（不可核实，工具会加 warning 标注）。",
    "qps_mean 是采样 QPS 的算术平均，不等于总请求数除以实际时长。",
    "缺失字段一律为 null；真实零值保留为 0。「没有发生」与「没有采集」请结合 "
    "summary_sources 与 missing 区分。",
]


def _vals(rows: list, key: str) -> list:
    return [r[key] for r in rows if r.get(key) is not None]


def _mean(vals: list) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _total(vals: list) -> Optional[float]:
    # 空列表 = 未采集（None）；非空 = 真实合计（可为 0）。不能用 `sum(...) or None`。
    return sum(vals) if vals else None


def _pct(vals: list, pct: float) -> Optional[float]:
    if not vals:
        return None
    vs = sorted(vals)
    return vs[min(len(vs) - 1, max(0, round(pct * (len(vs) - 1))))]


def _peak(vals: list) -> Optional[float]:
    return max(vals) if vals else None


def _last_non_none(rows: list, key: str):
    for r in reversed(rows):
        if r.get(key) is not None:
            return r[key]
    return None


def get_run_metrics(run_id: str) -> dict:
    """回答「这次压测表现如何」。run_id 自动归一化（8 位十六进制）。

    数据源：已保存报告（L0/L1）、Locust 历史 CSV、metrics 表（全部只读，
    不重新采集）。summary 字段实际来源见 summary_sources
    （saved_report / locust_history_csv / metrics_table / run_row），回退：
    报告非 None 用报告 → 历史 CSV 最后一行 → qps 再用 metrics 表。
    缺失字段一律 null（真实零值保留 0），不编造零请求/零失败/零错误率。
    """

    def _metrics():
        run = _fetch_run(normalized)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)

        warnings: list = []
        missing: list = []

        # ---- 数据源 1+3：reports 表与 metrics 表（同一只读连接）----
        report_row = None
        metrics_rows: list = []
        with _readonly_conn() as conn:
            try:
                report_row = conn.execute(
                    "SELECT l0_json, l1_json, created_at FROM reports WHERE run_id=?",
                    (normalized,),
                ).fetchone()
                metrics_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT ts, qps, avg_ms, p95_ms, p99_ms, err_rate, "
                        "threads_running, threads_connected, mysql_qps, slow_inc, "
                        "lock_waits_inc, tmp_disk_inc, bufpool_hit "
                        "FROM metrics WHERE run_id=? ORDER BY ts",
                        (normalized,),
                    ).fetchall()
                ]
            except sqlite3.Error as e:
                raise _ToolError(
                    "DATA_INVALID", f"读取 reports/metrics 表失败：{type(e).__name__}: {e}", False
                ) from e

        report = None
        if report_row is not None:
            try:
                report = {
                    "created_at": report_row["created_at"],
                    "l0": json.loads(report_row["l0_json"]),
                    "l1": json.loads(report_row["l1_json"]),
                }
            except (ValueError, TypeError):
                warnings.append("已保存报告存在但 JSON 无法解析，忽略并改用原始数据")

        # ---- 数据源 2：Locust 历史 CSV（严格解析）----
        hist_path = settings.data_dir / "locust" / f"{normalized}_stats_history.csv"
        hist = _read_last_history_strict(hist_path)
        per_sql_hist = _read_per_sql_history_strict(hist_path)

        sources = {
            "saved_report": report is not None,
            "locust_history_csv": hist is not None,
            "metrics_table": bool(metrics_rows),
            "per_sql_history_csv": bool(per_sql_hist),
        }
        for name in ("saved_report", "locust_history_csv", "metrics_table", "per_sql_history_csv"):
            if not sources[name]:
                missing.append(name)

        l0 = (report or {}).get("l0") or {}

        # ---- 汇总（报告优先，CSV/metrics 回退；不重算 L0）----
        def _pick(key: str, hist_key: Optional[str] = None) -> tuple:
            """saved_report → locust_history_csv 链：取第一个非 None；返回 (value, source)。"""
            if key in l0 and l0.get(key) is not None:
                return l0[key], "saved_report"
            v = (hist or {}).get(hist_key or key)
            if v is not None:
                return v, "locust_history_csv"
            return None, None

        summary: dict = {}
        summary_sources: dict = {}
        for key in ("total_requests", "total_failures", "err_rate", "avg_ms", "p95_ms", "p99_ms"):
            summary[key], summary_sources[key] = _pick(key)
        # 只在已保存报告里存在的字段（历史 CSV 最后一行没有这些列）
        for key in ("p50_ms", "max_ms", "threads_running_p95",
                    "mysql_slow_total", "mysql_lock_waits_total"):
            v = l0.get(key)
            summary[key] = v
            summary_sources[key] = "saved_report" if v is not None else None
        summary["qps_mean"], summary_sources["qps_mean"] = _pick("qps_avg", "qps")
        v = l0.get("qps_peak")
        summary["qps_peak"] = v
        summary_sources["qps_peak"] = "saved_report" if v is not None else None
        summary["duration_sec"] = run["duration_sec"]
        summary_sources["duration_sec"] = "run_row"
        summary["actual_duration_sec"], summary_sources["actual_duration_sec"] = _pick("actual_duration_sec")
        if summary_sources["actual_duration_sec"] is None:
            summary["actual_duration_sec"] = _iso_seconds(run.get("started_at"), run.get("ended_at"))
            if summary["actual_duration_sec"] is not None:
                summary_sources["actual_duration_sec"] = "run_row"

        # 汇总主来源（只有一个值，字段级以 summary_sources 为准）
        summary_source = "saved_report" if report is not None else (
            "locust_history_csv" if hist is not None else (
                "metrics_table" if metrics_rows else None
            )
        )

        # ---- metrics 表摘要（零值安全）----
        ts_values = [r["ts"] for r in metrics_rows]
        metrics_table_summary = {
            "point_count": len(metrics_rows),
            "ts_from": min(ts_values) if ts_values else None,
            "ts_to": max(ts_values) if ts_values else None,
            "span_sec": (max(ts_values) - min(ts_values)) if ts_values else None,
            "qps_mean": _mean(_vals(metrics_rows, "qps")),
            "qps_peak": _peak(_vals(metrics_rows, "qps")),
            "avg_ms_mean": _mean(_vals(metrics_rows, "avg_ms")),
            "err_rate_last": _last_non_none(metrics_rows, "err_rate"),
            "threads_running_mean": _mean(_vals(metrics_rows, "threads_running")),
            "threads_running_p95": _pct(_vals(metrics_rows, "threads_running"), 0.95),
            "threads_connected_last": _last_non_none(metrics_rows, "threads_connected"),
            "mysql_qps_total": _total(_vals(metrics_rows, "mysql_qps")),
            "mysql_slow_total": _total(_vals(metrics_rows, "slow_inc")),
            "mysql_lock_waits_total": _total(_vals(metrics_rows, "lock_waits_inc")),
            "mysql_tmp_disk_total": _total(_vals(metrics_rows, "tmp_disk_inc")),
            "bufpool_hit_last": _last_non_none(metrics_rows, "bufpool_hit"),
        }
        # 回退：没有报告/CSV 时，qps 用 metrics 表自己的口径（来源会标注）
        if summary["qps_mean"] is None and metrics_table_summary["qps_mean"] is not None:
            summary["qps_mean"] = metrics_table_summary["qps_mean"]
            summary_sources["qps_mean"] = "metrics_table"
        if summary["qps_peak"] is None and metrics_table_summary["qps_peak"] is not None:
            summary["qps_peak"] = metrics_table_summary["qps_peak"]
            summary_sources["qps_peak"] = "metrics_table"

        # ---- 报告侧缺列时把计数填 0（report.py 的 `_num(...) or 0`），
        # 无法用原始 CSV 核实的零值加 warning 标注，不猜测 ----
        if report is not None:
            for key in ("total_requests", "total_failures"):
                if l0.get(key) == 0:
                    if hist is None:
                        warnings.append(
                            f"已保存报告 {key}=0，但当前数据目录没有该 run 的 Locust 历史 CSV，"
                            "此零值无法核实（报告侧在计数字段缺失时会填 0）"
                        )
                    elif hist.get(key) is None:
                        warnings.append(
                            f"已保存报告 {key}=0，但历史 CSV 缺少对应计数字段，"
                            "此零值可能是报告侧的默认填充，未经核实"
                        )

        # ---- 按任务组/SQL ID 统计 ----
        try:
            tasks = resolve_tasks(run)
        except Exception as e:
            tasks = None
            warnings.append(
                _redact(f"任务组解析失败，per_sql 缺少语句数/权重信息：{type(e).__name__}: {e}")
            )
        task_meta = {
            t["sql_id"]: {
                "weight": t.get("weight"),
                "statement_count": len(t.get("statements") or []),
            }
            for t in (tasks or [])
        }
        report_per_sql = {
            s.get("sql_id"): s for s in (l0.get("per_sql") or []) if isinstance(s, dict)
        }
        per_sql: dict = {}
        for sql_id in sorted(set(task_meta) | set(report_per_sql) | set(per_sql_hist)):
            entry: dict = {}
            meta = task_meta.get(sql_id)
            if meta:
                entry.update(meta)
            rep = report_per_sql.get(sql_id)
            if rep:
                entry["report"] = {
                    k: rep.get(k)
                    for k in ("requests", "failures", "avg_ms", "p50_ms", "p95_ms",
                              "p99_ms", "max_ms", "qps")
                }
            series = per_sql_hist.get(sql_id)
            if series:
                entry["series"] = {
                    "points": len(series),
                    "rps_mean": _mean([p["rps"] for p in series if p.get("rps") is not None]),
                    # Failures/s 是速率不是次数：只给速率口径，不求和处理
                    "failure_rate_per_sec_mean": _mean([
                        p["failures_per_sec"] for p in series
                        if p.get("failures_per_sec") is not None]),
                    "failure_rate_per_sec_last": _last_non_none(series, "failures_per_sec"),
                    "avg_ms_last": _last_non_none(series, "avg_ms"),
                    "p95_ms_last": _last_non_none(series, "p95_ms"),
                    "p99_ms_last": _last_non_none(series, "p99_ms"),
                }
            per_sql[sql_id] = entry

        in_progress = run["status"] in ("pending", "running")
        data_available = any(sources.values())
        if in_progress:
            warnings.append(
                f"任务状态为 {run['status']}，数据仍在变化；以下为当前已落库数据的快照"
            )
        if not data_available:
            missing.append(
                "任务尚未结束，当前没有任何已落库数据" if in_progress
                else "任务已结束，但没有任何可用的指标数据（报告、Locust CSV、metrics 表均为空）"
            )

        data = {
            "run_id": run["id"],
            "name": run["name"],
            "status": run["status"],
            "in_progress": in_progress,
            "data_available": data_available,
            "sources": sources,
            "summary": summary,
            "summary_source": summary_source,
            "summary_sources": summary_sources,
            "saved_report": report,
            "csv_last_window": hist,
            "metrics_table_summary": metrics_table_summary if metrics_rows else None,
            "per_sql": per_sql,
            "missing": missing,
            "field_notes": {
                "summary_sources": {
                    "unit": "-",
                    "source": "本工具",
                    "caliber": "summary 每个字段的实际来源：saved_report / locust_history_csv / "
                               "metrics_table / run_row；null=无来源（值为 null）。"
                               "回退规则：报告字段非 None 用报告；否则用历史 CSV 最后一行；"
                               "qps 再用 metrics 表。",
                },
                "total_requests": {
                    "unit": "次",
                    "source": "locust stats.csv Aggregated（经已保存报告 L0 或历史 CSV）",
                    "caliber": "整个压测的累计请求数；一次请求对应一个 SQL 任务组，不等于单条 SQL "
                               "执行次数。saved_report 口径在 stats.csv 缺列时会填 0（不可核实"
                               "时会加 warning 标注）。",
                },
                "err_rate": {
                    "unit": "比例(0-1)",
                    "source": "已保存报告 L0 / locust 历史 CSV",
                    "caliber": "累计失败数/累计请求数；任一计数缺失或总请求为 0 时为 null，不编造 0。",
                },
                "avg_ms": {"unit": "毫秒", "caliber": "累计平均响应时间（任务组整体计时）。"
                             "metrics_table_summary.avg_ms_mean 是各采样点累计均值的算术平均，"
                             "只是参考值，不等于全程平均延迟。"},
                "p95_ms / p99_ms": {
                    "unit": "毫秒",
                    "caliber": "saved_report=stats.csv 最终统计（全程分位）；"
                               "locust_history_csv=最后一行窗口分位（只在报告缺该字段时回退，"
                               "来源见 summary_sources）。不把窗口 P99 冒充全程 P99，也不平均多个 P99。",
                },
                "qps_mean": {
                    "unit": "请求/秒",
                    "caliber": "saved_report=各采样 QPS 的算术平均；locust_history_csv=最后一行的"
                               "瞬时 Requests/s；metrics_table=采样点均值。不等于 总请求数/实际时长。",
                },
                "summary.mysql_slow_total / mysql_lock_waits_total": {
                    "unit": "次",
                    "caliber": "来自已保存报告 L0 的 col_sum（`sum(...) or None`：真实零值会显示为 "
                               "null）。可核实的合计看 metrics_table_summary.mysql_slow_total 等"
                               "（缺失为 null、真实 0 保留为 0）。",
                },
                "metrics_table_summary.mysql_qps_total": {
                    "unit": "次",
                    "caliber": "相邻采样差分的增量合计（增量求和=采样窗口内的次数），未按实际时间"
                               "归一化；SHOW GLOBAL STATUS 为实例级",
                },
                "metrics_table_summary.threads_running_*": {
                    "unit": "个",
                    "caliber": "采样时刻快照（Threads_running 是活动线程，不是已连接数）",
                },
                "per_sql.series.failure_rate_per_sec_*": {
                    "unit": "失败请求/秒",
                    "source": "locust *_stats_history.csv 的 Failures/s 列",
                    "caliber": "速率的均值/最后值，不是累计失败次数；本工具不把速率求和当次数。"
                               "累计失败数只存在于已保存报告（缺列时报告侧填 0，见 total_requests 口径）。",
                },
                "per_sql.report.requests / failures": {
                    "unit": "次",
                    "source": "已保存报告 L0（stats.csv 按 Name 分组）",
                    "caliber": "报告侧在缺列时填 0（report.py 口径），不可核实；"
                               "必要时对照 series 的速率数据。",
                },
            },
            "caveats": list(_CAVEATS_METRICS),
        }
        return _ok(normalized, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(
            _redact(str(run_id)),
            "INVALID_RUN_ID",
            "run_id 必须是 8 位十六进制（如 bb01ea65）",
            False,
        )
    return _wrap(normalized, _metrics)


# ---------------------------------------------------------------------------
# 工具 3：get_run_explain
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
    "产物 probe_ok 只表示采集时连上了目标库，不代表每条语句都有计划；"
    "语句级结果看 statement_status / has_plan / collection_status。",
]


def get_run_explain(run_id: str) -> dict:
    """回答「已有采样执行计划提供了什么证据」。run_id 自动归一化（8 位十六进制）。

    只读已有产物 ``data/explain/{run_id}.json``（不连接 MySQL、不重新采集）。
    外层 ``ok`` 只表示工具成功解析产物，不代表有执行计划；采集状态看
    ``probe_ok``（是否连上目标库）、``has_plan``、``collection_status``
    （complete / partial / failed / nothing_explainable / none；
    BEGIN/COMMIT 等被跳过不算失败）。错误路径：RUN_NOT_FOUND /
    EXPLAIN_NOT_READY（可重试）/ EXPLAIN_NOT_AVAILABLE（刚结束可能仍在收尾，
    可有限重试）/ EXPLAIN_CORRUPT。工具自身不循环等待。
    """

    def _explain():
        run = _fetch_run(normalized)
        if run is None:
            raise _ToolError("RUN_NOT_FOUND", "任务不存在", False)

        if run["status"] in ("pending", "running"):
            raise _ToolError(
                "EXPLAIN_NOT_READY",
                f"任务状态为 {run['status']}；EXPLAIN 在压测收尾时才采集，尚无产物",
                True,
            )

        path = artifact_path(normalized, out_dir=settings.data_dir / "explain")
        if not path.is_file():
            message = (
                "任务已结束，但没有 EXPLAIN 采集产物。可能原因：该任务当时的采集开关关闭、"
                "平台重启中断了收尾采集（收尾采集只对本进程启动的压测有效）、或采集时出错。"
                "这不代表压测失败，也不能据此判断采集是永久失败。"
            )
            retryable = False
            secs = _seconds_since(run.get("ended_at"))
            if secs is not None and secs < 300:
                retryable = True
                message += f" 任务刚结束约 {secs} 秒，产物可能仍在收尾写入，调用方可有限重试。"
            if not settings.explain_probe_enabled:
                message += (
                    " 注意：当前进程（本工具）配置 EXPLAIN_PROBE_ENABLED=false，"
                    "这只反映本工具此刻的配置，不代表该任务压测发生时 Web 进程的配置。"
                )
            raise _ToolError("EXPLAIN_NOT_AVAILABLE", message, retryable)

        artifact = load_artifact(normalized, out_dir=settings.data_dir / "explain")
        if not isinstance(artifact, dict):
            raise _ToolError(
                "EXPLAIN_CORRUPT",
                f"产物存在但无法解析（JSON 损坏或结构不是对象）：{path}",
                False,
            )
        if artifact.get("run_id") != normalized:
            raise _ToolError(
                "EXPLAIN_CORRUPT",
                f"产物记录的 run_id（{artifact.get('run_id')!r}）与请求的 {normalized} 不一致",
                False,
            )
        tasks = artifact.get("tasks")
        if not isinstance(tasks, list):
            raise _ToolError("EXPLAIN_CORRUPT", "产物缺少 tasks 列表（结构不完整）", False)
        summ = artifact.get("summary")
        if summ is not None and not isinstance(summ, dict):
            raise _ToolError("EXPLAIN_CORRUPT", "产物 summary 不是对象（结构不完整）", False)

        # 逐条检查 findings 的 code；结构损坏直接 EXPLAIN_CORRUPT，不静默跳过。
        statement_status = {
            "total": 0, "probed_ok": 0, "skipped_not_explainable": 0,
            "compile_error": 0, "explain_error": 0, "not_probed": 0, "other_failed": 0,
        }
        for t in tasks:
            if not isinstance(t, dict) or not isinstance(t.get("sql_id"), str):
                raise _ToolError("EXPLAIN_CORRUPT", "产物 tasks 含结构损坏的条目", False)
            stmts = t.get("statements")
            if not isinstance(stmts, list):
                raise _ToolError(
                    "EXPLAIN_CORRUPT", f"sql_id={t.get('sql_id')!r} 缺少 statements 列表", False
                )
            for st in stmts:
                if not isinstance(st, dict):
                    raise _ToolError("EXPLAIN_CORRUPT", "产物 statements 含非对象条目", False)
                if "ok" in st and not isinstance(st["ok"], bool):
                    raise _ToolError("EXPLAIN_CORRUPT", "语句条目 ok 字段类型不是布尔", False)
                if st.get("plan") is not None and not isinstance(st.get("plan"), list):
                    raise _ToolError("EXPLAIN_CORRUPT", "语句条目 plan 不是列表", False)
                findings = st.get("findings")
                if findings is not None and (
                    not isinstance(findings, list)
                    or any(not isinstance(f, dict) for f in findings)
                ):
                    raise _ToolError("EXPLAIN_CORRUPT", "语句条目 findings 结构损坏", False)
                statement_status["total"] += 1
                if st.get("ok"):
                    statement_status["probed_ok"] += 1
                    continue
                codes = {f.get("code") for f in (findings or [])}
                hit = False
                for key in ("skipped_not_explainable", "compile_error", "explain_error", "not_probed"):
                    if key in codes:
                        statement_status[key] += 1
                        hit = True
                if not hit:
                    statement_status["other_failed"] += 1

        probe_ok = artifact.get("ok") is True
        total = statement_status["total"]
        skipped = statement_status["skipped_not_explainable"]
        explainable = total - skipped
        failed_count = (
            statement_status["compile_error"] + statement_status["explain_error"]
            + statement_status["not_probed"] + statement_status["other_failed"]
        )
        has_plan = statement_status["probed_ok"] > 0
        if total == 0:
            collection_status = "none"
        elif explainable == 0:
            # 全部是不可 EXPLAIN 的语句（BEGIN/COMMIT/SET 等）：正常跳过，不算失败
            collection_status = "nothing_explainable"
        elif failed_count == 0:
            collection_status = "complete"
        elif has_plan:
            collection_status = "partial"
        else:
            collection_status = "failed"

        warnings: list = []
        if not probe_ok:
            warnings.append(
                "产物顶层 probe_ok=false：采集时连接/编译失败，probe_error 有原因；"
                "计划证据可能为空"
            )
        if summ.get("errors"):
            warnings.append(f"summary 记录 errors={summ['errors']}：部分语句采集失败")
        if failed_count:
            warnings.append(
                f"{total} 条语句中 {statement_status['probed_ok']} 条取得计划，"
                f"{failed_count} 条失败/达采集上限（{skipped} 条不可 EXPLAIN 为正常跳过，"
                "不计入失败）"
            )
        if total and skipped == total and not failed_count:
            warnings.append(
                f"{total} 条语句全部不可 EXPLAIN（事务控制/SET 等，正常跳过），"
                "本轮没有计划证据"
            )
        if artifact.get("truncated"):
            warnings.append(f"采集被截断：{artifact.get('truncated_reason')}")
        if not total:
            warnings.append("产物没有任何语句条目")

        target = artifact.get("target")
        data = {
            "run_id": artifact.get("run_id"),
            "present": True,
            # probe_ok 是产物记录的采集状态（连接是否成功），不是"有没有计划"
            "probe_ok": probe_ok,
            "has_plan": has_plan,
            "collection_status": collection_status,
            "probe_version": artifact.get("probe_version"),
            "phase": artifact.get("phase"),
            "generated_at": artifact.get("generated_at"),
            "server_version": artifact.get("server_version"),
            "probe_error": _redact(artifact.get("error")),
            # target 按白名单取（契约本身不含 password）
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
        return _ok(normalized, data, warnings)

    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(
            _redact(str(run_id)),
            "INVALID_RUN_ID",
            "run_id 必须是 8 位十六进制（如 bb01ea65）",
            False,
        )
    return _wrap(normalized, _explain)
