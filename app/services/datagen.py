import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger

from app import db
from app.config import settings
from app.services.runner import now_iso
from app.services.script_runner import run_script_job
from app.services.sql_executor import _log_line, execute_sql_job, split_sql_statements
from app.services.sql_grouping import GLOBAL_GROUP, group_statements
from app.services.sql_params import compile_statement, compile_variables
from app.services.datagen_dependencies import build_dependency_plan, build_parent_insert
from app.services.datagen_rules import read_table_metadata


def _table_log_path(log_path: Path, table: str) -> Path:
    """多表并发时每个分组写各自的日志文件，避免行交错后无法按表定位问题。"""
    name = "global" if table == GLOBAL_GROUP else re.sub(r"[^0-9A-Za-z_]+", "_", table).strip("_") or "table"
    return log_path.with_name(f"{log_path.stem}__{name}{log_path.suffix}")


class DataGenExecutor:
    def __init__(self):
        self._cancel: dict = {}
        self._lock = threading.Lock()

    def start(self, job_id: str) -> None:
        with self._lock:
            job = db.get_datagen_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job["status"] != "pending":
                raise ValueError(f"datagen job {job_id} 状态为 {job['status']}，无法启动")

            job_dir = settings.data_dir / "datagen" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            log_path = settings.logs_dir / f"datagen_{job_id}.log"
            db.update_datagen_job(
                job_id,
                {"status": "running", "started_at": now_iso(), "log_path": str(log_path)},
            )
            event = threading.Event()
            self._cancel[job_id] = event

        try:
            threading.Thread(target=self._worker, args=(job_id, event), daemon=True).start()
        except Exception as e:
            with self._lock:
                self._cancel.pop(job_id, None)
            db.update_datagen_job(
                job_id,
                {"status": "failed", "error_msg": f"创建后台线程失败：{e}", "ended_at": now_iso()},
            )
            raise
        logger.info("datagen job {} started", job_id)

    def stop(self, job_id: str) -> str:
        with self._lock:
            job = db.get_datagen_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job["status"] != "running":
                raise PermissionError(f"datagen job {job_id} 状态为 {job['status']}，无法停止")
            event = self._cancel.get(job_id)
            db.update_datagen_job(
                job_id,
                {"status": "cancelled", "ended_at": now_iso()},
            )
            if event is not None:
                event.set()
        logger.info("datagen job {} stop requested", job_id)
        return "cancelled"

    def _worker(self, job_id: str, event: threading.Event) -> None:
        try:
            job = db.get_datagen_job(job_id)
            dsn = json.loads(job["target_dsn_json"])
            payload = json.loads(job["input_json"])
            job_dir = settings.data_dir / "datagen" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            log_path = Path(job["log_path"]) if job.get("log_path") else settings.logs_dir / f"datagen_{job_id}.log"
            content = payload.get("content") or ""

            if job["mode"] == "multi":
                (job_dir / "input.sql").write_text(content, encoding="utf-8")
                status, rows, error = self._run_multi(job_id, dsn, payload, log_path, event)
            elif job["mode"] == "sql":
                (job_dir / "input.sql").write_text(content, encoding="utf-8")
                statements = split_sql_statements(content)
                definitions = compile_variables(payload.get("variables") or {})
                compiled = [compile_statement(stmt, definitions, f"造数 SQL 第 {i} 条") for i, stmt in enumerate(statements, 1)]
                dependency_strategy = payload.get("dependency_strategy", "target_only")
                dependency_plan = payload.get("dependency_plan") or {}
                if dependency_strategy != "target_only":
                    if not payload.get("confirm_dependency_writes"):
                        raise ValueError("自动补充依赖表必须先确认依赖表写入")
                    if not payload.get("target_table"):
                        raise ValueError("自动补充依赖表需要目标表")
                    dependency_plan = build_dependency_plan(dsn, payload["target_table"], int(payload.get("row_count") or 1), dependency_strategy)
                    if dependency_plan.get("warnings") and not dependency_plan.get("execution_order"):
                        raise ValueError("依赖计划不可执行：" + "；".join(dependency_plan["warnings"]))
                    dependency_items = dependency_plan.get("dependencies") or []
                    order_index = {table: index for index, table in enumerate(dependency_plan.get("execution_order") or [])}
                    dependency_items = sorted(dependency_items, key=lambda item: order_index.get(item["table"], 0))
                    for item in dependency_items:
                        rows = int(item.get("planned_rows") or 0)
                        if rows <= 0:
                            continue
                        table = item["table"]
                        logger.info("[execute] generating dependency table {} rows={}", table, rows)
                        metadata = read_table_metadata(dsn, table)
                        parent_sql = build_parent_insert(metadata)
                        parent_compiled = [compile_statement(parent_sql, {}, f"依赖表 {table}")]
                        parent_status, parent_rows, parent_error = execute_sql_job(
                            job_id, dsn, parent_compiled, log_path, event,
                            settings.datagen_sql_timeout_sec, row_count=rows,
                            variable_definitions={}, target_table=table,
                            unique_indexes=metadata.get("unique_indexes") or [],
                        )
                        logger.info("[execute] finished dependency table {} affected={}", table, parent_rows)
                        if parent_status != "finished":
                            status, rows, error = parent_status, parent_rows, parent_error
                            break
                    else:
                        status, rows, error = execute_sql_job(
                            job_id, dsn, compiled, log_path, event,
                            settings.datagen_sql_timeout_sec,
                            row_count=int(payload.get("row_count") or 1),
                            variable_definitions=definitions,
                            target_table=payload.get("target_table"),
                            unique_indexes=payload.get("unique_indexes") or [],
                        )
                else:
                    status, rows, error = execute_sql_job(
                        job_id, dsn, compiled, log_path, event,
                        settings.datagen_sql_timeout_sec,
                        row_count=int(payload.get("row_count") or 1),
                        variable_definitions=definitions,
                        target_table=payload.get("target_table"),
                        unique_indexes=payload.get("unique_indexes") or [],
                    )
            else:
                status, rows, error = run_script_job(
                    job_id,
                    dsn,
                    content,
                    job_dir,
                    log_path,
                    event,
                    settings.datagen_script_timeout_sec,
                )

            fields = {"rows_affected": rows, "ended_at": now_iso()}
            if status == "cancelled":
                fields["status"] = "cancelled"
            elif status in ("finished", "partial"):
                fields["status"] = status
                if error:
                    fields["error_msg"] = error
            else:
                fields["status"] = "failed"
                fields["error_msg"] = error
            db.update_datagen_job(job_id, fields)
            logger.info("datagen job {} finished status={} rows={}", job_id, fields["status"], rows)
        except Exception as e:
            logger.exception("datagen job {} worker failed", job_id)
            db.update_datagen_job(
                job_id,
                {"status": "failed", "error_msg": str(e), "ended_at": now_iso()},
            )
        finally:
            with self._lock:
                self._cancel.pop(job_id, None)

    def _run_multi(self, job_id: str, dsn: dict, payload: dict, log_path: Path,
                   event: threading.Event) -> tuple:
        """多表导入：按目标表分组，组内串行、组间并发；单组失败不影响其他分组。"""
        statements = split_sql_statements(payload.get("content") or "")
        if not statements:
            return "failed", 0, "没有可执行的 SQL 语句"
        definitions = compile_variables(payload.get("variables") or {})
        compiled = [compile_statement(stmt, definitions, f"多表 SQL 第 {i} 条")
                    for i, stmt in enumerate(statements, 1)]
        groups = group_statements(compiled)
        targets = [(table, items) for table, items in groups if table != GLOBAL_GROUP]
        controls = next((items for table, items in groups if table == GLOBAL_GROUP), [])
        if not targets:
            return "failed", 0, "没有识别出任何目标表，多表导入至少需要一条能识别目标表的语句"

        concurrency = max(1, min(int(payload.get("concurrency") or 4), len(targets),
                                 settings.datagen_max_concurrency))
        row_count = max(1, int(payload.get("row_count") or 1))
        _log_line(log_path, f"[{job_id}] 多表模式：{len(statements)} 条语句 → {len(targets)} 个表分组，"
                            f"并发度 {concurrency}，每条 SQL 重复 {row_count} 次")
        if controls:
            _log_line(log_path, f"[{job_id}] {len(controls)} 条语句无法识别目标表（SET/BEGIN 等），"
                                f"将在独立连接中先行执行，不影响其他分组的会话")

        results: dict = {}
        lock = threading.Lock()

        def run_group(table: str, items: list) -> None:
            table_log = _table_log_path(log_path, table)
            status, rows, error = execute_sql_job(
                job_id, dsn, items, table_log, event, settings.datagen_sql_timeout_sec,
                row_count=row_count, variable_definitions=definitions,
                target_table=None if table == GLOBAL_GROUP else table,
                unique_indexes=[],
            )
            with lock:
                results[table] = {"status": status, "rows": rows or 0,
                                  "error": error, "statements": len(items)}
            _log_line(log_path, f"[{job_id}] {table} {status} 影响 {rows or 0} 行"
                                + (f"，原因：{error}" if error else ""))

        if controls and not event.is_set():
            run_group(GLOBAL_GROUP, controls)

        if not event.is_set():
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(run_group, table, items): table for table, items in targets}
                for future in as_completed(futures):
                    table = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        logger.exception("datagen multi group {} failed", table)
                        with lock:
                            results.setdefault(table, {"status": "failed", "rows": 0,
                                                       "error": str(exc), "statements": 0})

        rows_total = sum(item["rows"] for item in results.values())
        if event.is_set():
            _log_line(log_path, f"[{job_id}] 多表模式已取消：{len(results)} 个分组已结束，影响 {rows_total} 行")
            return "cancelled", rows_total, None
        failed = [(table, item) for table, item in sorted(results.items()) if item["status"] != "finished"]
        if not failed:
            _log_line(log_path, f"[{job_id}] 多表模式完成：{len(results)} 个分组全部成功，共影响 {rows_total} 行")
            return "finished", rows_total, None
        detail = "；".join(f"{table}: {item['error'] or item['status']}" for table, item in failed)
        _log_line(log_path, f"[{job_id}] 多表模式结束：{len(results) - len(failed)} 个分组成功，"
                            f"{len(failed)} 个分组失败")
        return ("failed" if len(failed) == len(results) else "partial"), rows_total, detail[:1000]


datagen_executor = DataGenExecutor()
