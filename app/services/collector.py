import asyncio
import csv as csvmod
import re
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


def _num(row: dict, keys: list, default=None):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "N/A"):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def read_last_history(csv_path: Path) -> Optional[dict]:
    """读 locust *_stats_history.csv 最后一行 Aggregated，列名做版本兼容。"""
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = [r for r in csvmod.DictReader(f) if r.get("Name") == "Aggregated"]
    except OSError:
        return None
    if not rows:
        return None
    r = rows[-1]
    total = _num(r, ["Total Request Count", "Request Count"], 0) or 0
    fail = _num(r, ["Total Failure Count", "Failure Count"], 0) or 0
    return {
        "qps": _num(r, ["Requests/s", "Current RPS"]),
        "avg_ms": _num(r, ["Total Average Response Time", "Average Response Time"]),
        "p95_ms": _num(r, ["95%"]),
        "p99_ms": _num(r, ["99%"]),
        "err_rate": (fail / total) if total else None,
        "total_requests": total,
        "total_failures": fail,
    }


_SQL_NAME_RE = re.compile(r"sql_\d+")


def read_per_sql_history(csv_path: Path) -> dict:
    """读 locust --csv-full-history 的 *_stats_history.csv，按语句返回时序。

    返回 {sql_id: [{ts, rps, fails, avg_ms, p95_ms, p99_ms}, ...]}，
    其中 p95/p99 为当前窗口分位（适合实时观察），avg 为累计均值。
    """
    out: dict = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csvmod.DictReader(f):
                name = r.get("Name", "")
                if not _SQL_NAME_RE.fullmatch(name):
                    continue  # 跳过 Aggregated / 其它条目
                ts = _num(r, ["Timestamp"])
                if ts is None:
                    continue
                out.setdefault(name, []).append({
                    "ts": int(ts),
                    "rps": _num(r, ["Requests/s", "Current RPS"]),
                    "fails": _num(r, ["Failures/s"], 0),
                    "avg_ms": _num(r, ["Total Average Response Time", "Average Response Time"]),
                    "p95_ms": _num(r, ["95%"]),
                    "p99_ms": _num(r, ["99%"]),
                })
    except OSError:
        return {}
    return out


class Collector:
    def __init__(self):
        self.subscribers: dict = {}  # run_id -> list[asyncio.Queue]
        self._mysql_conn = None
        self._mysql_dsn = None
        self._last_status: dict = {}  # run_id -> 上一次 SHOW GLOBAL STATUS 快照
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
            self._last_status.pop(run_id, None)
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

    def _mysql_metrics(self, run_id: str, status: dict) -> dict:
        """与该 run 上一次快照差分，得到每秒增量指标。"""
        out = {}
        prev = self._last_status.get(run_id)
        if prev:
            for key in COUNTER_KEYS:
                if key in status and key in prev:
                    out_key = COUNTER_FIELDS.get(key)
                    if out_key:
                        out[out_key] = max(0, status[key] - prev[key])
        # buffer pool 命中率（累计口径，非差分）
        reads = status.get("Innodb_buffer_pool_reads")
        requests = status.get("Innodb_buffer_pool_read_requests")
        if reads is not None and requests is not None and requests > 0:
            out["bufpool_hit"] = (requests - reads) / requests
        self._last_status[run_id] = status
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
        hist = read_last_history(settings.data_dir / "locust" / f"{run_id}_stats_history.csv")
        dsn = decrypt_dsn_snapshot(jsonmod.loads(run["db_dsn_json"]))
        status = None
        try:
            status = self._query_status(dsn)
        except Exception as e:
            logger.debug("mysql status query failed: {}", e)
        if hist is None and status is None:
            return None

        host, _host_error = self._host_metrics(run_id)

        point = {
            "run_id": run_id,
            "ts": int(time.time()),
            "metric_scope": "window",
            "qps": (hist or {}).get("qps"),
            "avg_ms": (hist or {}).get("avg_ms"),
            "p95_ms": (hist or {}).get("p95_ms"),
            "p99_ms": (hist or {}).get("p99_ms"),
            "err_rate": (hist or {}).get("err_rate"),
        }
        exception_summary = read_exception_summary(run_id)
        top_exception = exception_summary["top_exception"] or {}
        point.update({
            "exception_count": exception_summary["exception_count"],
            "user_start_failures": exception_summary["user_start_failures"],
            "failure_events": exception_summary["failure_events"],
            "top_exception_message": top_exception.get("message"),
            "top_exception_count": top_exception.get("count"),
            "request_count": (hist or {}).get("total_requests"),
            "request_failures": (hist or {}).get("total_failures"),
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
            point.update(self._mysql_metrics(run_id, status))
        point.update(host)
        return point

    async def run_loop(self) -> None:
        logger.info("collector loop started")
        while True:
            t0 = time.perf_counter()
            try:
                running = [r for r in db.list_runs() if r["status"] == "running"]
                for run in running:
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
