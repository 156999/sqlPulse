"""压测结束后的 EXPLAIN 采集。

动机：`app/services/rule_engine.py` 的规则文案里写着"用 EXPLAIN 确认执行计划"，
但项目从来没有真的执行过一次 EXPLAIN —— 诊断链路一直是"只看结果不看原因"。
本模块把这句话变成一段真实采集。

采集时机（2026-09-25 从"压测启动前"改为"压测结束后"）
-----------------------------------------------------
调用点：`runner.py::LocustRunner._probe_explain_after_run()`，位于 run 状态落库之后、
`_on_finish`（生成报告）**之前**。

之所以能放在后面而不损失任何东西：`EXPLAIN` 问的是"优化器打算怎么执行"，
**优化器走完就停、执行器不启动**，答案在压测前后是同一个对象。
反过来这么摆有两个好处：

- 不再同步阻塞启动（原来最多要占掉 `EXPLAIN_BUDGET_SEC` 秒）。
- 不存在"产物已经有了、但压测还没跑"的游离状态。

代价见 `docs/design/explain-collection.md`「采集时机」：产物依赖 run 走到收尾，
所以**服务重启或进程硬崩溃的 run 采不到**（`db.recover_orphans()` 会把它们直接标
`failed`，而 `_watch()` 只对本进程 Popen 出来的进程有效，根本不会跑）。
这是刻意接受的取舍，不是疏漏。

三条硬约束（没变）：

1. **只读**。只用 `EXPLAIN`（绝不是 `EXPLAIN ANALYZE`）—— 优化器走完就停，
   执行器不启动，不读数据、不加锁，可以安全地跑在用户的库上。
   唯一的例外见下面「关于占位符取值」。
2. **不阻断**。采集失败（连不上、语法错、语句不可 EXPLAIN）只记录，
   绝不影响报告生成。
3. **不碰 DB schema**。结果落到 `data/explain/{run_id}.json`，与
   `data/locust/{run_id}_stats.csv` 是同一种模式：引擎产生产物，下游按约定读。
   `runs` 表没有迁移机制（`db.py` 的 `_METRIC_ALTERS` 只覆盖 `metrics`），能绕开就绕开。

关于占位符取值（`feature-0916` 起的重要变化）
------------------------------------------------
占位符已从「运行期文本替换」改为「编译期参数绑定」（`app/services/sql_params.py`）。
所以采集不再自己拼字符串，而是复用压测运行期的同一条链路：

    compile_tasks() → Statement.bind(values) → cursor.mogrify(query, args)

`bind()` 产出 `(%s, ...)` 形式的语句与取值，`mogrify()` 把它渲染成一条可发送的
完整 SQL —— 送进 EXPLAIN 的就是压测**真正会执行**的那条语句（除了取值是样本）。

⚠️ 别把"同一条链路"读成"压测跑过的那些语句"：压测期间发出的 SQL **一条都没有被记录**，
locust 子进程只回传统计（`sql_N` 的耗时/失败），不回传语句文本或取值。
所以采集是**按 run 行里存的原始 SQL 模板重放一次**，不是抓取：

    db.get_run() → resolve_tasks(run) → compile_tasks() → 现场取一组值 → EXPLAIN

模板同源（`resolve_tasks` 与 `start()` 共用），取值重新生成 —— 所以是"同一条 SQL 的
另一个采样点"，不是"压测中某一次请求的原样复现"。

因此存在一处**读操作**：`sample(...)` 类占位符需要先从目标表读样本
（`SampleCache`，`SELECT ... LIMIT n`，只读、有上限）。没有 `sample` 占位符时不会发生。

消费方式（报告侧）：

    from app.services.explain_probe import load_artifact
    data = load_artifact(run_id)   # 不存在时返回 None，调用方必须容忍

产物结构（稳定契约，`probe_version` 随结构变更递增）：

    {
      "run_id", "probe_version", "phase", "generated_at",
      "target": {host, port, database, user},      # 刻意不含 password
      "ok", "error", "server_version", "notes",
      "truncated", "truncated_reason",
      "summary": {tasks, statements, probed, skipped_not_explainable,
                  dropped_by_limit, errors, findings, by_code,
                  worst_level, max_rows, max_rows_ref, elapsed_ms},
      "tasks": [{sql_id, weight, statements: [
          {index, original, filled, parameters, ok,
           plan: [ ...列白名单... ], max_rows, findings}
      ]}]
    }

`phase` 说明这份产物是什么时机采的（`post_run`）。目前只有这一个取值，
留出来是为了以后若要补一轮"压测前"的采集时，下游能区分两份产物的来源。

`findings[].code` 取值：`table_scan` / `index_ignored` / `full_index_scan` /
`filesort` / `temporary` / `join_buffer`（计划质量类，参与 `worst_level`）；
`skipped_not_explainable` / `not_probed`（事实记录类，不参与 `worst_level`）；
`compile_error` / `explain_error`（计划质量类：出了问题，参与 `worst_level`）。

粒度：EXPLAIN 只能逐条发，而压测账本按 **task / `sql_id`** 记
（`sql_user.py.j2` 里一个 task 一个 `@task`，跑完 task 内所有语句只上报一次计时）。
产物用 `tasks[].statements[]` 的双层结构把细的挂回粗的那层 ——
消费方看到"sql_2 慢"时，要按 `sql_id` 取 `statements[]` 再逐条展开，**不能假设一对一**。
"""
import json
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import pymysql
from loguru import logger

from app.services.sql_executor import SampleCache
from app.services.sql_params import compile_tasks

PROBE_VERSION = 3

# 采集时机。目前只有"压测结束后"一种；留出常量是为了以后加"压测前"时下游能区分。
PHASE_POST_RUN = "post_run"

# EXPLAIN 只讲"读路径"。这些开场词不可 EXPLAIN，遇到就跳过并记录原因。
#
# ⚠️ 必须是白名单，不要"化简"成黑名单（只排除事务控制词）：MySQL 里
# `EXPLAIN <名字>` 同时是 `DESCRIBE <名字>` 的同义词，所以 `EXPLAIN BEGIN`
# 不会被拒成"不支持 EXPLAIN"，而是把 BEGIN 当**表名**去解析 —— 实测 8.0.46 返回
# `1146 Table 'sqlpulse_demo.BEGIN' doesn't exist`（`EXPLAIN COMMIT` / `START` /
# `ROLLBACK` / `SAVEPOINT` 同理）；`SET` / `LOCK TABLES` / `SHOW` / `CALL` /
# `TRUNCATE` / `USE` 则是 1064 语法错。
# 两类错误都会被记成 `explain_error`（level=warn，**参与 worst_level**），
# 于是每一轮带事务的压测都会凭空报出几处"计划问题"。
# 跳过则记 `skipped_not_explainable`（bookkeeping，不影响 worst_level）—— 这才是对的。
_EXPLAINABLE = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE")

# 计划表里只保留这些列（拍成契约，MySQL 加列不影响产物结构）
_PLAN_COLS = (
    "id", "select_type", "table", "type", "possible_keys",
    "key", "key_len", "ref", "rows", "filtered", "Extra",
)

# 纯事实记录类的 findings，不参与 worst_level（否则"跳过了 BEGIN"会把等级顶到 info，
# 让 worst_level 失去意义 —— 它应该只反映"计划本身有没有坏味道"）。
_BOOKKEEPING = frozenset({"skipped_not_explainable", "not_probed"})

_LEADING_COMMENT_RE = re.compile(r"^(?:\s*(?:/\*.*?\*/|--[^\n]*|#[^\n]*))+", re.S)

_LEVEL_RANK = {"info": 0, "warn": 1, "error": 2}

DEFAULT_MAX_STATEMENTS = 50
DEFAULT_BUDGET_SEC = 15.0

NOTES = [
    "采集发生在压测结束之后，计划反映的是那一刻库里的统计信息。"
    "只读压测下与压测前没有区别；夹带写入的压测会让表的统计信息重算，计划可能已经变了 —— "
    "要判断这一点，看目标表此刻的 TABLE_ROWS 与 EXPLAIN 里的 rows 是否吻合。",
    "rows 是优化器的估算值，不是真实扫描行数；表刚批量导入后统计信息可能过期，"
    "必要时对目标表执行 ANALYZE TABLE 再复跑。",
    "EXPLAIN 只解释读路径。INSERT/UPDATE/DELETE 的 EXPLAIN 不会真的执行，"
    "写路径的锁竞争要看压测采集到的 lock_waits_inc。",
    "这里展示的 SQL 是压测真正会发送的那条（占位符已完成参数绑定），"
    "但取值是样本：普通随机占位符由 run_id 决定种子、同一个 run 可复现，"
    "sample(...) 类则取决于目标表当时的数据。它代表「典型值」的计划形态，不代表极端值。",
]


def artifact_path(run_id: str, out_dir: Optional[Path] = None) -> Path:
    base = Path(out_dir) if out_dir is not None else _default_out_dir()
    return base / f"{run_id}.json"


def load_artifact(run_id: str, out_dir: Optional[Path] = None) -> Optional[dict]:
    """读采集产物；不存在或损坏时返回 None（消费方必须容忍它缺失）。"""
    try:
        return json.loads(artifact_path(run_id, out_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _default_out_dir() -> Path:
    # 延迟 import：本模块要能被单测在没有 .env / 没有 data 目录时导入
    from app.config import settings

    return settings.data_dir / "explain"


def _connect(dsn: dict):
    return pymysql.connect(
        host=dsn["host"], port=int(dsn["port"]), user=dsn["user"],
        password=dsn["password"], database=dsn["database"],
        connect_timeout=3, read_timeout=10, autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextmanager
def _seeded(seed: str):
    """临时给模块级 random 定种，保证同一个 run 的样本值可复现。

    `sql_params.Generator.sample()` 用的是模块级 `random`（`rand`/`pick`/`randstr` ...），
    而采集是在服务进程里跑的，所以只能临时替换全局状态并在退出时**原样还原**，
    避免影响同进程里的其他随机（例如并发的造数任务）。
    """
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


def first_keyword(stmt: str) -> str:
    """取语句的首个关键字（跳过前置注释）。"""
    s = _LEADING_COMMENT_RE.sub("", stmt or "").strip()
    return s.split(None, 1)[0].upper() if s else ""


def is_explainable(stmt: str) -> bool:
    kw = first_keyword(stmt)
    return kw in _EXPLAINABLE


def classify_plan(rows: list) -> list:
    """把一张 EXPLAIN 计划表翻译成 findings。

    判定只用四列：type → key → rows → Extra（见 docs/onboarding/explain-primer.md）。
    """
    out = []
    for r in rows or []:
        table = r.get("table") or "?"
        # 大小写敏感点：MySQL 原样输出 `ALL` 是大写、`index`/`const` 是小写。
        # 统一 upper 之后比较时，字面量也必须是大写（否则 type=index 会被静默漏判）。
        acc = (r.get("type") or "").upper()
        key = r.get("key")
        possible = r.get("possible_keys")
        extra = r.get("Extra") or ""
        nrows = r.get("rows")

        if acc == "ALL":
            out.append({
                "code": "table_scan", "level": "warn", "table": table,
                "detail": f"表 {table} 全表扫描（type=ALL，预估 {nrows} 行）。",
            })
        elif acc == "INDEX":
            out.append({
                "code": "full_index_scan", "level": "info", "table": table,
                "detail": f"表 {table} 扫了整个索引树（type=index，预估 {nrows} 行），"
                          f"比全表略好，但仍不是定位查询。",
            })

        if not key and possible:
            out.append({
                "code": "index_ignored", "level": "warn", "table": table,
                "detail": f"表 {table} 有可用索引（{possible}）但优化器没用它（key=NULL）。"
                          f"常见原因：类型不匹配、函数包住了列、或前置通配 LIKE '%x'。",
            })

        low = str(extra).lower()
        for token, code, detail in (
            ("using filesort", "filesort", f"表 {table} 额外做了一次排序（ORDER BY 未走索引）。"),
            ("using temporary", "temporary", f"表 {table} 建了临时表（GROUP BY / DISTINCT 常见）。"),
            ("using join buffer", "join_buffer", f"表 {table} 的 join 未走索引，靠内存缓冲硬凑。"),
        ):
            if token in low:
                out.append({"code": code, "level": "warn", "table": table, "detail": detail})
    return out


def _max_rows(rows: list) -> Optional[int]:
    vals = [r.get("rows") for r in rows or [] if isinstance(r.get("rows"), (int, float))]
    return int(max(vals)) if vals else None


def _typed(value: Any) -> dict:
    """把绑定值变成 JSON 安全的 {type, value}。

    刻意不复用 `run_form.typed`：`run_form` 反向 import 了 `runner`，
    而 `runner` 要 import 本模块 → 会形成循环 import。
    """
    if value is None:
        return {"type": "null", "value": "NULL"}
    if type(value) is bool:
        return {"type": "bool", "value": "1" if value else "0"}
    if type(value) is int:
        return {"type": "int", "value": str(value)}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, (Decimal, datetime, date)):
        return {"type": type(value).__name__, "value": str(value)}
    # 兜底：任何非 JSON 原生类型都转成字符串，避免整份产物因序列化失败而丢失
    return {"type": type(value).__name__, "value": repr(value)}


def _values_for(definitions: dict, sample_cache: Optional[SampleCache]) -> dict:
    """为一个 task 生成一组取值。

    ⚠️ 与压测运行期**只对齐到"一 task 一组"这一层**，取值容器并不相同，别照抄压测侧：

        sql_user.py.j2 L31   values = {name: g.sample({}) ...}   ← 空 dict，不注入 __sample__
        本函数                values["__sample__"] = ... 后再 sample(values)

    差别只在 `sample(...)` 这类占位符上，且是刻意的：采集侧自己持有目标库连接，
    可以安全地读样本（`SampleCache`，只读 + LIMIT）；而压测 worker 那边
    `values["__sample__"]` 缺席，`Generator.sample()` 会直接抛
    `sample 占位符需要任务级采样缓存` —— 表现为「EXPLAIN 采集正常、压测 100% 失败」
    （实测 run `f24aa749` 269/269、run `f70c365` 同现象）。这是**上游缺陷、未修**；
    要不要修取决于"worker 是否允许额外查目标表"这个设计决策，不属本模块。
    纯 CPython 层面的最小复现见 `tests/manual/verify_param_parity.py`。
    """
    values: dict = {}
    if sample_cache is not None:
        values["__sample__"] = sample_cache.sample
    values.update({name: g.sample(values) for name, g in (definitions or {}).items()})
    return values


def probe(
    run_id: str,
    tasks: list,
    dsn: dict,
    *,
    variables: Optional[dict] = None,
    connector: Optional[Callable[[dict], Any]] = None,
    out_dir: Optional[Path] = None,
    seed: Optional[str] = None,
    max_statements: int = DEFAULT_MAX_STATEMENTS,
    budget_sec: float = DEFAULT_BUDGET_SEC,
    phase: str = PHASE_POST_RUN,
) -> dict:
    """对 tasks 里的每条语句跑 EXPLAIN，落盘并返回结果。**本函数不抛异常。**

    :param variables: 变量定义（表单的 `variables`），与压测同源。
    :param connector: 连接工厂，默认 `_connect`。测试可注入假连接，从而离线跑全流程。
    """
    result: dict = {
        "run_id": run_id,
        "probe_version": PROBE_VERSION,
        "phase": phase,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target": {
            "host": dsn.get("host"), "port": dsn.get("port"),
            "database": dsn.get("database"), "user": dsn.get("user"),
        },  # 刻意不含 password
        "ok": False,
        "error": None,
        "server_version": None,
        "notes": list(NOTES),
        "truncated": False,
        "truncated_reason": None,
        "summary": {},
        "tasks": [],
    }
    connect = connector or _connect
    started = time.monotonic()

    # 先编译。失败说明占位符有问题 —— 但那属于提交流程该拦的错，
    # 这里只记录，绝不阻断压测（`runner.start()` 后面自己还会编译一次并决定成败）。
    try:
        definitions, compiled = compile_tasks(tasks or [], variables or {})
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("explain probe: compile failed for run {}: {}", run_id, e)
        _finish(result, started, 0, 0, 0, 0, {}, {})
        _write(result, artifact_path(run_id, out_dir))
        return result

    conn = None
    try:
        conn = connect(dsn)
    except Exception as e:  # 连不上不算崩，只记录
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("explain probe: connect failed for run {}: {}", run_id, e)

    sample_cache = None
    if conn is not None:
        result["ok"] = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                row = cur.fetchone() or {}
                result["server_version"] = next(iter(row.values()), None)
        except Exception as e:  # 版本取不到不影响采集本身
            logger.warning("explain probe: SELECT VERSION() failed: {}", e)
        sample_cache = SampleCache(conn)

    probed = skipped_na = dropped = errors = 0
    by_code: dict = {}
    worst_seen = False
    worst_level = "info"
    worst_rows: Optional[int] = None
    worst_ref: Optional[dict] = None
    stopped_by: Optional[str] = None

    def note(code: str, level: str) -> None:
        nonlocal worst_seen, worst_level
        by_code[code] = by_code.get(code, 0) + 1
        if code in _BOOKKEEPING:
            return
        worst_seen = True
        if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(worst_level, 0):
            worst_level = level

    if conn is not None:
        try:
            with _seeded(seed if seed is not None else f"sqlpulse-probe:{run_id}"):
                for t in tasks or []:
                    sql_id = t.get("sql_id")
                    t_out = {"sql_id": sql_id, "weight": t.get("weight"), "statements": []}
                    statements = compiled.get(sql_id, [])
                    # 一个 task 内的语句共用一组取值 —— 与运行期一致
                    # （`sql_user.py.j2::_exec` 每次请求只生成一次 values）。
                    try:
                        values = _values_for(definitions, sample_cache)
                    except Exception as e:
                        values = None
                        error = e
                    else:
                        error = None

                    for i, stmt in enumerate(t.get("statements") or []):
                        entry: dict = {"index": i, "original": stmt, "filled": None,
                                       "parameters": [], "ok": False, "plan": None,
                                       "max_rows": None, "findings": []}

                        if not is_explainable(stmt):
                            kw = first_keyword(stmt) or "?"
                            entry["findings"] = [{
                                "code": "skipped_not_explainable", "level": "info", "table": None,
                                "detail": f"`{kw}` 不可 EXPLAIN（事务控制、SET 等没有执行计划），未采集。",
                            }]
                            skipped_na += 1
                        elif values is None:
                            entry["findings"] = [{
                                "code": "compile_error", "level": "warn", "table": None,
                                "detail": f"占位符取值失败：{error}",
                            }]
                            errors += 1
                        else:
                            # 额度与预算都在"发出 EXPLAIN 之前"判，避免超发
                            if probed >= max_statements:
                                stopped_by = stopped_by or "max_statements"
                            elif time.monotonic() - started >= budget_sec:
                                stopped_by = stopped_by or "budget_sec"
                            if stopped_by:
                                entry["findings"] = [{
                                    "code": "not_probed", "level": "info", "table": None,
                                    "detail": f"已达采集上限（{stopped_by}），本条未做 EXPLAIN。",
                                }]
                                dropped += 1
                            else:
                                # 取值与 EXPLAIN 必须各自兜住：任何一条语句出错都不能
                                # 中断整轮采集（例如 sample 表为空、目标列不存在）。
                                # 两者分开 try，是为了让 findings 的 code 说清是哪一步坏了。
                                compiled_stmt = statements[i] if i < len(statements) else None
                                if compiled_stmt is None:
                                    entry["findings"] = [{
                                        "code": "compile_error", "level": "warn", "table": None,
                                        "detail": "这条语句没有编译结果，未采集。",
                                    }]
                                    errors += 1
                                else:
                                    try:
                                        query, args = compiled_stmt.bind(values)
                                        with conn.cursor() as cur:
                                            # mogrify 把 %s 参数渲染成字面量：
                                            # 送进 EXPLAIN 的就是压测真正会发送的那条语句。
                                            full_sql = cur.mogrify(query, tuple(args or ()))
                                    except Exception as e:
                                        entry["findings"] = [{
                                            "code": "compile_error", "level": "warn", "table": None,
                                            "detail": f"占位符取值失败：{e}",
                                        }]
                                        errors += 1
                                    else:
                                        entry["filled"] = full_sql
                                        entry["parameters"] = [_typed(v) for v in args or ()]
                                        try:
                                            with conn.cursor() as cur:
                                                cur.execute("EXPLAIN " + full_sql)
                                                rows = list(cur.fetchall() or [])
                                        except Exception as e:
                                            entry["findings"] = [{
                                                "code": "explain_error", "level": "warn", "table": None,
                                                "detail": f"EXPLAIN 失败：{e}",
                                            }]
                                            errors += 1
                                        else:
                                            plan = [{c: r.get(c) for c in _PLAN_COLS} for r in rows]
                                            entry["plan"] = plan
                                            entry["ok"] = True
                                            entry["max_rows"] = _max_rows(plan)
                                            entry["findings"] = classify_plan(plan)
                                            probed += 1
                                            if entry["max_rows"] is not None and (
                                                worst_rows is None or entry["max_rows"] > worst_rows
                                            ):
                                                worst_rows = entry["max_rows"]
                                                worst_ref = {"sql_id": sql_id, "index": i}

                        for f in entry["findings"]:
                            note(f["code"], f["level"])
                        t_out["statements"].append(entry)
                    result["tasks"].append(t_out)
        except Exception as e:
            logger.warning("explain probe: aborted for run {}: {}", run_id, e)
            result["error"] = result["error"] or f"{type(e).__name__}: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if stopped_by:
        result["truncated"] = True
        result["truncated_reason"] = (
            f"max_statements={max_statements}" if stopped_by == "max_statements"
            else f"budget_sec={budget_sec}"
        )

    _finish(result, started, skipped_na, dropped, errors, probed, by_code,
            {"worst_level": worst_level if worst_seen else None,
             "max_rows": worst_rows, "max_rows_ref": worst_ref})
    _write(result, artifact_path(run_id, out_dir))
    logger.info(
        "explain probe {}: probed={} skipped={} dropped={} errors={} worst={}",
        run_id, probed, skipped_na, dropped, errors, result["summary"]["worst_level"],
    )
    return result


def _finish(result: dict, started: float, skipped_na: int, dropped: int, errors: int,
            probed: int, by_code: dict, extra: dict) -> None:
    extra = extra or {}   # 容错：调用方漏传不该把整轮采集炸掉
    result["summary"] = {
        "tasks": len(result["tasks"]),
        "statements": sum(len(t["statements"]) for t in result["tasks"]),
        "probed": probed,
        "skipped_not_explainable": skipped_na,
        "dropped_by_limit": dropped,
        "errors": errors,
        "findings": sum(by_code.values()),
        "by_code": by_code,
        "worst_level": extra.get("worst_level"),
        "max_rows": extra.get("max_rows"),
        "max_rows_ref": extra.get("max_rows_ref"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _write(data: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # 原子替换，避免下游读到半截 JSON
    except (OSError, TypeError, ValueError) as e:
        # probe() 承诺不抛异常，所以序列化问题也必须在这里被吃掉
        logger.warning("explain probe: write {} failed: {}", path, e)
