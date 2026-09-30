import json
import threading
from pathlib import Path

from loguru import logger

from app import db
from app.config import settings
from app.services.runner import now_iso
from app.services.script_runner import run_script_job
from app.services.sql_executor import execute_sql_job, split_sql_statements
from app.services.sql_params import compile_statement, compile_variables
from app.services.datagen_dependencies import build_dependency_plan, build_parent_insert
from app.services.datagen_rules import read_table_metadata


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

            if job["mode"] == "sql":
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
            elif status == "finished":
                fields["status"] = "finished"
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


datagen_executor = DataGenExecutor()
