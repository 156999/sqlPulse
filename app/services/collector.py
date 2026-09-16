import asyncio
import csv as csvmod
import re
import time
from pathlib import Path
from typing import Optional

import pymysql
from loguru import logger

from app import db
from app.config import settings

# 需要差分为"每秒增量"的计数器（SHOW GLOBAL STATUS 键名）
COUNTER_KEYS = [
    "Questions", "Slow_queries", "Innodb_row_lock_waits",
    "Created_tmp_disk_tables", "Com_commit",
]


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
                    out_key = {
                        "Questions": "mysql_qps",
                        "Slow_queries": "slow_inc",
                        "Innodb_row_lock_waits": "lock_waits_inc",
                        "Created_tmp_disk_tables": "tmp_disk_inc",
                    }.get(key)
                    if out_key:
                        out[out_key] = max(0, status[key] - prev[key])
        # buffer pool 命中率（累计口径，非差分）
        reads = status.get("Innodb_buffer_pool_reads")
        requests = status.get("Innodb_buffer_pool_read_requests")
        if reads is not None and requests is not None and requests > 0:
            out["bufpool_hit"] = (requests - reads) / requests
        self._last_status[run_id] = status
        return out

    def sample(self, run: dict) -> Optional[dict]:
        import json as jsonmod

        run_id = run["id"]
        hist = read_last_history(settings.data_dir / "locust" / f"{run_id}_stats_history.csv")
        dsn = jsonmod.loads(run["db_dsn_json"])
        status = None
        try:
            status = self._query_status(dsn)
        except Exception as e:
            logger.debug("mysql status query failed: {}", e)
        if hist is None and status is None:
            return None

        point = {
            "run_id": run_id,
            "ts": int(time.time()),
            "qps": (hist or {}).get("qps"),
            "avg_ms": (hist or {}).get("avg_ms"),
            "p95_ms": (hist or {}).get("p95_ms"),
            "p99_ms": (hist or {}).get("p99_ms"),
            "err_rate": (hist or {}).get("err_rate"),
        }
        if status:
            point["threads_running"] = status.get("Threads_running")
            point["threads_connected"] = status.get("Threads_connected")
            point.update(self._mysql_metrics(run_id, status))
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
