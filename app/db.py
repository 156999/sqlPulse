import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.config import settings

_local = threading.local()
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  status        TEXT NOT NULL,
  sql_source    TEXT NOT NULL,
  sql_content   TEXT NOT NULL,
  concurrency   INTEGER NOT NULL,
  spawn_rate    INTEGER NOT NULL,
  duration_sec  INTEGER NOT NULL,
  db_dsn_json   TEXT NOT NULL,
  error_msg     TEXT,
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  ended_at      TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  run_id        TEXT NOT NULL,
  ts            INTEGER NOT NULL,
  qps           REAL,
  avg_ms        REAL,
  p95_ms        REAL,
  p99_ms        REAL,
  err_rate      REAL,
  threads_running   INTEGER,
  threads_connected INTEGER,
  mysql_qps     REAL,
  slow_inc      REAL,
  lock_waits_inc REAL,
  tmp_disk_inc  REAL,
  bufpool_hit   REAL,
  PRIMARY KEY (run_id, ts)
);
CREATE TABLE IF NOT EXISTS reports (
  run_id        TEXT PRIMARY KEY,
  l0_json       TEXT NOT NULL,
  l1_json       TEXT NOT NULL,
  md_path       TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(str(settings.data_dir / "sqlpulse.db"), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
        return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


_METRIC_COLS = [
    "qps", "avg_ms", "p95_ms", "p99_ms", "err_rate",
    "threads_running", "threads_connected",
    "mysql_qps", "slow_inc", "lock_waits_inc", "tmp_disk_inc", "bufpool_hit",
]
# 旧库升级：缺列则补
_METRIC_ALTERS = {
    "mysql_qps": "REAL", "slow_inc": "REAL", "lock_waits_inc": "REAL",
    "tmp_disk_inc": "REAL", "bufpool_hit": "REAL",
}


def init_schema() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
        for col, typ in _METRIC_ALTERS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")


def recover_orphans() -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE runs SET status='failed', error_msg='平台重启中断', ended_at=datetime('now') WHERE status IN ('pending','running')"
        )
        return cur.rowcount


def create_run(row: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO runs (id,name,status,sql_source,sql_content,concurrency,spawn_rate,duration_sec,db_dsn_json,created_at)"
            " VALUES (:id,:name,:status,:sql_source,:sql_content,:concurrency,:spawn_rate,:duration_sec,:db_dsn_json,:created_at)",
            row,
        )


def get_run(run_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_run(run_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    fields = dict(fields)
    fields["id"] = run_id
    with tx() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id=:id", fields)


def insert_metric(m: dict) -> None:
    cols = [c for c in _METRIC_COLS if c in m]
    with tx() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO metrics (run_id,ts,{','.join(cols)}) VALUES (:run_id,:ts,{':' + ',:'.join(cols)})",
            {**{c: None for c in _METRIC_COLS}, **m},
        )


def get_metrics(run_id: str, ts_from: Optional[int] = None, ts_to: Optional[int] = None) -> list:
    conn = get_conn()
    sql = "SELECT * FROM metrics WHERE run_id=?"
    args: list[Any] = [run_id]
    if ts_from is not None:
        sql += " AND ts>=?"
        args.append(ts_from)
    if ts_to is not None:
        sql += " AND ts<=?"
        args.append(ts_to)
    sql += " ORDER BY ts"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def save_report(run_id: str, l0: dict, l1: list, md_path: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reports (run_id,l0_json,l1_json,md_path,created_at) VALUES (?,?,?,?,datetime('now'))",
            (run_id, json.dumps(l0, ensure_ascii=False), json.dumps(l1, ensure_ascii=False), md_path),
        )


def get_report(run_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM reports WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["l0"] = json.loads(d.pop("l0_json"))
    d["l1"] = json.loads(d.pop("l1_json"))
    return d
