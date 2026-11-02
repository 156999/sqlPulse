"""Read complete Locust records incrementally; keep SQL time separate from sampling time."""

import csv
import math
import os
import re
import threading
import time
from pathlib import Path

from app.config import settings

SQL_NAME_RE = re.compile(r"sql_\d+")
SQL_FIELDS = ("qps", "avg_ms", "p95_ms", "p99_ms", "err_rate", "request_count", "request_failures")
METRIC_SCOPES = {
    "qps": "locust_recent_completed_seconds",
    "p95_ms": "locust_rolling_10s",
    "p99_ms": "locust_rolling_10s",
    "avg_ms": "cumulative",
    "err_rate": "cumulative",
    "request_count": "cumulative",
    "request_failures": "cumulative",
}


def number(row, keys, default=None):
    for key in keys:
        try:
            value = float(row.get(key))
            if math.isfinite(value):
                return value
        except (TypeError, ValueError):
            pass
    return default


class HistoryReader:
    """Only advance past newline-terminated rows; retry a partially flushed row next time."""

    def __init__(self):
        self.offset = 0
        self.header = None
        self.identity = None

    def read(self, path: Path) -> list[dict]:
        rows = []
        try:
            with path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if identity != self.identity or stat.st_size < self.offset:
                    self.offset, self.header = 0, None
                self.identity = identity
                handle.seek(self.offset)
                while True:
                    line = handle.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    self.offset = handle.tell()
                    try:
                        values = next(csv.reader([line.decode("utf-8-sig")], strict=True))
                    except (UnicodeError, csv.Error):
                        continue
                    if self.header is None:
                        self.header = values
                        continue
                    if len(values) != len(self.header):
                        continue
                    row = dict(zip(self.header, values))
                    ts = number(row, ["Timestamp"])
                    count = number(row, ["Total Request Count", "Request Count"])
                    failures = number(row, ["Total Failure Count", "Failure Count"])
                    if ts is not None and ts >= 0 and count is not None and failures is not None:
                        rows.append(row)
        except OSError:
            pass
        return rows


def aggregate_point(row: dict) -> dict:
    total = number(row, ["Total Request Count", "Request Count"], 0)
    failures = number(row, ["Total Failure Count", "Failure Count"], 0)
    ts = int(number(row, ["Timestamp"]))
    return {
        "ts": ts, "source_ts": ts, "metric_scope": "sql",
        "qps": number(row, ["Requests/s", "Current RPS"]),
        "avg_ms": number(row, ["Total Average Response Time", "Average Response Time"]),
        "p95_ms": number(row, ["95%"]), "p99_ms": number(row, ["99%"]),
        "err_rate": failures / total if total else None,
        "request_count": total, "request_failures": failures,
    }


def read_per_sql_history(path: Path) -> dict:
    series = {}
    for row in HistoryReader().read(path):
        name = row.get("Name", "")
        if not SQL_NAME_RE.fullmatch(name):
            continue
        ts = int(number(row, ["Timestamp"]))
        series.setdefault(name, {})[ts] = {
            "ts": ts,
            "rps": number(row, ["Requests/s", "Current RPS"]),
            "fails": number(row, ["Failures/s"], 0),
            "avg_ms": number(row, ["Total Average Response Time", "Average Response Time"]),
            "p95_ms": number(row, ["95%"]), "p99_ms": number(row, ["99%"]),
        }
    return {name: [points[ts] for ts in sorted(points)] for name, points in series.items()}


_readers = {}
_lock = threading.RLock()


def sync_sql_history(run_id: str, historical: bool = False) -> list[dict]:
    """Persist all newly flushed aggregate rows once, including backfills after restart.

    Historical imports have no known original acquisition time: leave collected_at NULL.
    SQL rows live in their own table, so backfill never rewrites resource measurements.
    """
    from app import db

    path = settings.data_dir / "locust" / f"{run_id}_stats_history.csv"
    with _lock:
        key = str(path.resolve())
        reader = _readers.setdefault(key, HistoryReader())
        try:
            rows = reader.read(path)
            points = [aggregate_point(row) for row in rows if row.get("Name") == "Aggregated"]
            return db.insert_sql_metrics(run_id, points, None if historical else time.time())
        except Exception:
            # The file offset is not a committed checkpoint if the DB write failed.
            _readers.pop(key, None)
            raise
