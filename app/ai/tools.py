"""SQLPulse 只读诊断工具。

前三个工具（``get_run_context`` / ``get_run_metrics`` / ``get_run_explain``）只读平台数据：
平台 SQLite、Locust CSV 与 EXPLAIN 产物，**不连目标 MySQL**。
``get_table_profile`` 是唯一例外：按需只读目标库的元数据（列 / 索引 / 表规模估算），
必须显式开启 ``AI_DB_PROBE_ENABLED``，且连接只从 run 快照解析、不接受外部指定。

公共约束：只读、不写文件与库、run_id 校验挡路径穿越、统一信封
``{ok, run_id, data, warnings, error}``，错误对象 ``{code, message, retryable}``。
"""
import json
import math
import re
import sqlite3
import urllib.parse
from contextlib import contextmanager
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


def get_run_metrics(run_id: str) -> dict:
    """读取累计 stats.csv 与已有报告；不生成报告、不重新采集。

    stats.csv 提供请求统计；报告提供已计算的采样摘要及 L1。
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
            warnings.append("运行中数据是读取时快照，CSV 与报告可能来自不同时间")
        warnings.append("已有报告的增量合计可能把真实零写成 null；本工具保留该缺失状态")
        return _ok(run_id, {
            "run_id": run_id, "status": run["status"], "in_progress": in_progress,
            "data_available": aggregate is not None or bool(per_sql) or bool(l0),
            "summary": summary, "summary_sources": sources,
            "per_sql": per_sql, "per_sql_source": "locust_stats_csv",
            "l1_findings": l1, "report_created_at": report_time,
            "missing": [key for key, value in summary.items() if value is None],
            "field_notes": {
                "requests": "一次 Locust 请求对应整个任务组，不是单条 SQL；延迟单位 ms",
                "err_rate": "累计失败数/累计请求数，范围 0 到 1；零请求时为 null",
                "qps": "stats.csv 的 Requests/s；不能与报告的采样 QPS 均值混为同一口径",
                "qps_avg": "已有报告中的采样请求速率算术平均",
                "mysql": "实例级增量，不能直接归因于本次压测或某条 SQL",
                "l1_findings": "已有规则结果，仅作为诊断输入，不等同确定根因",
            },
        }, warnings)
    normalized = _norm_run_id(run_id)
    if normalized is None:
        return _fail(None, "INVALID_RUN_ID", "run_id 必须是 8 位十六进制", False)
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
# 工具 4：get_table_profile（本模块唯一连目标库的工具，需显式开关）
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
