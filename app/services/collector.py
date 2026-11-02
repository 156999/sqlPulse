import asyncio
import time
from pathlib import Path
from typing import Optional

import pymysql
from loguru import logger

try:
    import psutil
except ImportError:  # pragma: no cover - requirements install psutil in normal deployments
    psutil = None

from app import db
from app.config import settings
from app.services.credential_crypto import decrypt_dsn_snapshot
from app.services.observability import read_exception_summary
from app.services.sql_history import HistoryReader, aggregate_point, read_per_sql_history, sync_sql_history

# 需要差分为"每秒增量"的计数器（SHOW GLOBAL STATUS 键名）
COUNTER_KEYS = [
    "Questions", "Slow_queries", "Innodb_row_lock_waits",
    "Innodb_row_lock_time", "Innodb_deadlocks", "Innodb_log_waits",
    "Innodb_log_writes", "Innodb_log_write_requests", "Created_tmp_disk_tables", "Com_commit",
]

COUNTER_FIELDS = {
    "Questions": "mysql_qps",
    "Slow_queries": "slow_inc",
    "Innodb_row_lock_waits": "lock_waits_inc",
    "Innodb_row_lock_time": "lock_wait_ms_inc",
    "Innodb_deadlocks": "deadlocks_inc",
    "Innodb_log_waits": "innodb_log_waits_inc",
    "Innodb_log_writes": "innodb_log_writes_inc",
    "Innodb_log_write_requests": "innodb_log_write_requests_inc",
    "Created_tmp_disk_tables": "tmp_disk_inc",
}


def read_last_history(csv_path: Path) -> Optional[dict]:
    """Compatibility helper; live ingestion uses every new source row, not just this one."""
    rows = [r for r in HistoryReader().read(csv_path) if r.get("Name") == "Aggregated"]
    if not rows:
        return None
    point = aggregate_point(rows[-1])
    return {**point, "total_requests": point["request_count"], "total_failures": point["request_failures"]}


class Collector:
    def __init__(self):
        self.subscribers: dict = {}  # run_id -> list[asyncio.Queue]
        self._mysql_conn = None
        self._mysql_dsn = None
        self._last_status: dict = {}  # run_id -> 上一次 SHOW GLOBAL STATUS 快照
        self._last_status_time: dict = {}
        self._last_host_io: dict = {}  # run_id -> host IO/network counters
        if psutil:
            psutil.cpu_percent(interval=None)  # prime the non-blocking CPU sampler

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
        self.subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        qs = self.subscribers.get(run_id, [])
        if q in qs:
            qs.remove(q)
        if not qs:
            self.subscribers.pop(run_id, None)

    def release_run(self, run_id: str) -> None:
        self._last_status.pop(run_id, None)
        self._last_status_time.pop(run_id, None)
        self._last_host_io.pop(run_id, None)

    def _broadcast(self, run_id: str, point: dict) -> None:
        for q in list(self.subscribers.get(run_id, [])):
            try:
                q.put_nowait(point)
            except asyncio.QueueFull:
                pass

    def _query_status(self, dsn: dict) -> Optional[dict]:
        """SHOW GLOBAL STATUS 全量取一次，返回所需键的 int dict；连接失败返回 None。"""
        if self._mysql_conn is None or self._mysql_dsn != dsn:
            self._mysql_dsn = dsn
            if self._mysql_conn:
                try:
                    self._mysql_conn.close()
                except Exception:
                    pass
            self._mysql_conn = pymysql.connect(
                host=dsn["host"], port=int(dsn["port"]), user=dsn["user"],
                password=dsn["password"], database=dsn["database"], connect_timeout=2,
            )
        try:
            with self._mysql_conn.cursor() as cur:
                cur.execute("SHOW GLOBAL STATUS")
                out = {}
                for k, v in cur.fetchall():
                    try:
                        out[k] = int(v)
                    except (TypeError, ValueError):
                        continue  # 跳过非数值状态（TLS 证书串 / ON-OFF / 时间戳等）
                cur.execute("SHOW GLOBAL VARIABLES LIKE 'max_connections'")
                row = cur.fetchone()
                if row:
                    try:
                        out["max_connections"] = int(row[1])
                    except (TypeError, ValueError):
                        pass
                try:
                    cur.execute(
                        "SELECT COUNT(DISTINCT USER) FROM information_schema.PROCESSLIST "
                        "WHERE USER IS NOT NULL AND USER <> '' AND COMMAND <> 'Sleep'"
                    )
                    row = cur.fetchone()
                    if row:
                        out["active_users"] = int(row[0])
                except Exception:
                    # PROCESS privilege is not required for the rest of SHOW GLOBAL STATUS.
                    pass
                return out
        except Exception:
            self._mysql_conn = None
            return None

    def _mysql_metrics(self, run_id: str, status: dict, sampled_at: Optional[float] = None) -> dict:
        """与该 run 上一次快照差分，得到每秒增量指标。"""
        out = {}
        now = time.monotonic() if sampled_at is None else sampled_at
        previous_time = self._last_status_time.get(run_id)
        elapsed = now - previous_time if previous_time is not None else None
        prev = self._last_status.get(run_id)
        if prev and elapsed is not None and elapsed > 0:
            out["mysql_sample_interval_s"] = elapsed
            for key in COUNTER_KEYS:
                if key in status and key in prev:
                    out_key = COUNTER_FIELDS.get(key)
                    if out_key and status[key] >= prev[key]:
                        out[out_key] = (status[key] - prev[key]) / elapsed
        # buffer pool 命中率（累计口径，非差分）
        reads = status.get("Innodb_buffer_pool_reads")
        requests = status.get("Innodb_buffer_pool_read_requests")
        if reads is not None and requests is not None and requests > 0:
            out["bufpool_hit"] = (requests - reads) / requests
        self._last_status[run_id] = status
        self._last_status_time[run_id] = now
        out["lock_current_waits"] = status.get("Innodb_row_lock_current_waits")
        out["innodb_log_pending_fsyncs"] = status.get("Innodb_os_log_pending_fsyncs")
        out["innodb_log_pending_writes"] = status.get("Innodb_os_log_pending_writes")
        return out

    def _host_metrics(self, run_id: str) -> tuple[dict, Optional[str]]:
        if psutil is None:
            return {}, "host_metrics_unavailable"
        try:
            now = time.monotonic()
            io = psutil.disk_io_counters()
            net = psutil.net_io_counters()
            previous = self._last_host_io.get(run_id)
            elapsed = max(now - previous["now"], 0.001) if previous else None
            out = {
                "cpu_pct": float(psutil.cpu_percent(interval=None)),
                "memory_pct": float(psutil.virtual_memory().percent),
                "memory_used_mb": psutil.virtual_memory().used / 1024 / 1024,
                "disk_used_pct": float(psutil.disk_usage(str(settings.data_dir)).percent),
            }
            if io and previous and elapsed:
                out["disk_read_mb_s"] = max(0, io.read_bytes - previous["read_bytes"]) / elapsed / 1024 / 1024
                out["disk_write_mb_s"] = max(0, io.write_bytes - previous["write_bytes"]) / elapsed / 1024 / 1024
            if net and previous and elapsed:
                out["net_recv_mb_s"] = max(0, net.bytes_recv - previous["recv_bytes"]) / elapsed / 1024 / 1024
                out["net_sent_mb_s"] = max(0, net.bytes_sent - previous["sent_bytes"]) / elapsed / 1024 / 1024
            self._last_host_io[run_id] = {
                "now": now,
                "read_bytes": io.read_bytes if io else 0,
                "write_bytes": io.write_bytes if io else 0,
                "recv_bytes": net.bytes_recv if net else 0,
                "sent_bytes": net.bytes_sent if net else 0,
            }
            return out, None
        except Exception as exc:
            logger.debug("host metrics query failed: {}", exc)
            return {}, "host_metrics_unavailable"

    def sample(self, run: dict) -> Optional[dict]:
        import json as jsonmod

        run_id = run["id"]
        dsn = decrypt_dsn_snapshot(jsonmod.loads(run["db_dsn_json"]))
        status = None
        try:
            status = self._query_status(dsn)
        except Exception as e:
            logger.debug("mysql status query failed: {}", e)
        mysql_sampled_at = time.monotonic()
        host, _host_error = self._host_metrics(run_id)
        collected_at = time.time()
        point = {
            "run_id": run_id,
            "ts": collected_at,
            "resource_collected_at": collected_at,
            "metric_scope": "resource",
        }
        exception_summary = read_exception_summary(run_id)
        top_exception = exception_summary["top_exception"] or {}
        point.update({
            "exception_count": exception_summary["exception_count"],
            "user_start_failures": exception_summary["user_start_failures"],
            "failure_events": exception_summary["failure_events"],
            "top_exception_message": top_exception.get("message"),
            "top_exception_count": top_exception.get("count"),
        })
        if status:
            point["threads_running"] = status.get("Threads_running")
            point["threads_connected"] = status.get("Threads_connected")
            point["max_connections"] = status.get("max_connections")
            point["active_users"] = status.get("active_users")
            if status.get("max_connections") and status.get("Threads_connected") is not None:
                point["connection_usage_pct"] = (
                    status["Threads_connected"] / status["max_connections"] * 100
                )
            point.update(self._mysql_metrics(run_id, status, mysql_sampled_at))
        point.update(host)
        return point

    async def run_loop(self) -> None:
        logger.info("collector loop started")
        while True:
            t0 = time.perf_counter()
            try:
                running = [r for r in db.list_runs() if r["status"] == "running"]
                active_ids = {run["id"] for run in running}
                for run_id in set(self._last_status) | set(self._last_host_io):
                    if run_id not in active_ids:
                        self.release_run(run_id)
                for run in running:
                    points = await asyncio.get_event_loop().run_in_executor(None, sync_sql_history, run["id"])
                    for sql_point in points:
                        self._broadcast(run["id"], sql_point)
                    point = await asyncio.get_event_loop().run_in_executor(None, self.sample, run)
                    if point:
                        db.insert_metric(point)
                        self._broadcast(run["id"], point)
            except Exception as e:
                logger.exception("collect loop error: {}", e)
            elapsed = time.perf_counter() - t0
            logger.debug("collect loop took {:.0f}ms", elapsed * 1000)
            await asyncio.sleep(max(0.1, 1.0 - elapsed))


collector = Collector()
