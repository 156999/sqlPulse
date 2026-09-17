import json
import threading
from pathlib import Path

from loguru import logger

from app import db
from app.config import settings
from app.services.runner import now_iso
from app.services.script_runner import run_script_job
from app.services.sql_executor import execute_sql_job, split_sql_statements


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
                status, rows, error = execute_sql_job(
                    job_id,
                    dsn,
                    statements,
                    log_path,
                    event,
                    settings.datagen_sql_timeout_sec,
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
