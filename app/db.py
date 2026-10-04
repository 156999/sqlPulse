import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional
from uuid import uuid4

from app.config import settings

_local = threading.local()
_conn: Optional[sqlite3.Connection] = None
# 全进程共用 _conn 这一条连接，所以**读和写都必须拿这把锁**（用 RLock 是因为
# 读函数可能被 tx() 内部再次调用）。见下面 _fetch_one/_fetch_all 的说明。
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  status        TEXT NOT NULL,
  sql_source    TEXT NOT NULL,
  sql_content   TEXT NOT NULL,
  variables_json TEXT NOT NULL DEFAULT '{}',
  groups_json TEXT NOT NULL DEFAULT '[]',
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
CREATE TABLE IF NOT EXISTS datagen_jobs (
  id                TEXT PRIMARY KEY,
  owner_user_id     TEXT,
  name              TEXT NOT NULL,
  mode              TEXT NOT NULL,
  status            TEXT NOT NULL,
  target_dsn_json   TEXT NOT NULL,
  input_json        TEXT NOT NULL,
  rows_affected     INTEGER,
  log_path          TEXT,
  error_msg         TEXT,
  created_at        TEXT NOT NULL,
  started_at        TEXT,
  ended_at          TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mysql_connections (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  name        TEXT NOT NULL,
  host        TEXT NOT NULL,
  port        INTEGER NOT NULL DEFAULT 3306,
  user        TEXT NOT NULL,
  password    TEXT NOT NULL DEFAULT '',
  database    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_mysql_connections_user
  ON mysql_connections (user_id);
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


# ---------------------------------------------------------------------------
# 所有「读」都必须走下面两个函数，**不能直接 conn.execute()**。
#
# 原因（2026-09-25 实测，代价是用户被偶发登出）：全进程只有 _conn 一条连接
# （check_same_thread=False），而 Python 的 sqlite3.Connection 不是为并发设计的。
# 一旦多个请求线程同时在这条连接上 execute，就会撞出
#     sqlite3.InterfaceError: bad parameter or other API misuse
# 更阴的是它**不一定抛异常**——有时直接给出一个假的空结果。
# 实测 12 线程读 + 1 线程写、共 4800 次查询：12.9% 抛 InterfaceError、11.3% 凭空返回 None。
#
# 为什么这个是「登出」而不是「500」：get_user() 假返回 None →
# auth.get_current_user() 认为用户不存在 → session.pop("user_id") →
# SessionMiddleware 看到会话被清空 → 回 Set-Cookie: ...=null; expires=1970
# → 浏览器删掉 cookie → 用户莫名其妙被登出。
#
# 所以：**别为了性能把这里的锁去掉**。SQLite 本来就是串行的，锁的代价可以忽略。
# ---------------------------------------------------------------------------

def _fetch_one(sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _lock:
        return conn.execute(sql, args).fetchone()


def _fetch_all(sql: str, args: tuple = ()) -> list:
    conn = get_conn()
    with _lock:
        return conn.execute(sql, args).fetchall()


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
        run_columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        if "groups_json" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN groups_json TEXT NOT NULL DEFAULT '[]'")
        if "variables_json" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN variables_json TEXT NOT NULL DEFAULT '{}'")
        existing = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
        for col, typ in _METRIC_ALTERS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")
        datagen_columns = {r[1] for r in conn.execute("PRAGMA table_info(datagen_jobs)")}
        if "owner_user_id" not in datagen_columns:
            conn.execute("ALTER TABLE datagen_jobs ADD COLUMN owner_user_id TEXT")
    seed_root_user()


def get_user_by_username(username: str) -> Optional[dict]:
    row = _fetch_one(
        "SELECT * FROM users WHERE username=? COLLATE NOCASE",
        (username,),
    )
    return dict(row) if row else None


def get_user(user_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    return dict(row) if row else None


def create_user(username: str, password_hash: str) -> dict:
    user_id = uuid4().hex
    created_at = datetime.now().isoformat(timespec="seconds")
    with tx() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, username, password_hash, created_at),
        )
    return {"id": user_id, "username": username, "created_at": created_at}


def seed_root_user() -> Optional[dict]:
    if not settings.auth_seed_root or get_user_by_username("root"):
        return None
    from app.auth import hash_password
    try:
        return create_user("root", hash_password("root"))
    except sqlite3.IntegrityError:
        # 多 worker 同时启动时可能同时看到 root 不存在，插入竞争由 UNIQUE 约束兜底。
        return get_user_by_username("root")


def list_connections(user_id: str) -> list:
    rows = _fetch_all(
        "SELECT * FROM mysql_connections WHERE user_id=? ORDER BY updated_at DESC, name ASC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def get_connection(user_id: str, connection_id: str) -> Optional[dict]:
    row = _fetch_one(
        "SELECT * FROM mysql_connections WHERE id=? AND user_id=?",
        (connection_id, user_id),
    )
    return dict(row) if row else None


def create_connection(user_id: str, fields: dict) -> dict:
    with tx() as c:
        params = dict(fields)
        params["user_id"] = user_id
        c.execute(
            "INSERT INTO mysql_connections"
            " (id,user_id,name,host,port,user,password,database,created_at,updated_at)"
            " VALUES (:id,:user_id,:name,:host,:port,:user,:password,:database,:created_at,:updated_at)",
            params,
        )
        row = c.execute(
            "SELECT * FROM mysql_connections WHERE id=? AND user_id=?",
            (params["id"], user_id),
        ).fetchone()
    return dict(row)


def update_connection(user_id: str, connection_id: str, fields: dict) -> Optional[dict]:
    with tx() as c:
        fields = {k: v for k, v in fields.items() if k != "updated_at"}
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        params = dict(fields)
        params["connection_id"] = connection_id
        params["user_id"] = user_id
        params["updated_at"] = datetime.now().isoformat(timespec="seconds")
        cur = c.execute(
            f"UPDATE mysql_connections SET {sets}, updated_at=:updated_at"
            " WHERE id=:connection_id AND user_id=:user_id",
            params,
        )
        if cur.rowcount == 0:
            return None
    return get_connection(user_id, connection_id)


def delete_connection(user_id: str, connection_id: str) -> bool:
    with tx() as c:
        cur = c.execute(
            "DELETE FROM mysql_connections WHERE id=? AND user_id=?",
            (connection_id, user_id),
        )
        return cur.rowcount > 0

def recover_orphans() -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE runs SET status='failed', error_msg='平台重启中断', ended_at=datetime('now') WHERE status IN ('pending','running')"
        )
        return cur.rowcount


def recover_datagen_orphans() -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE datagen_jobs SET status='failed', error_msg='平台重启中断', ended_at=datetime('now') "
            "WHERE status IN ('pending','running')"
        )
        return cur.rowcount


def create_run(row: dict) -> None:
    row = {"variables_json": "{}", "groups_json": "[]", **row}
    with tx() as conn:
        conn.execute(
            "INSERT INTO runs (id,name,status,sql_source,sql_content,variables_json,groups_json,concurrency,spawn_rate,duration_sec,db_dsn_json,created_at)"
            " VALUES (:id,:name,:status,:sql_source,:sql_content,:variables_json,:groups_json,:concurrency,:spawn_rate,:duration_sec,:db_dsn_json,:created_at)",
            row,
        )


def get_run(run_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    return dict(row) if row else None


def list_runs() -> list:
    rows = _fetch_all("SELECT * FROM runs ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def update_run(run_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    fields = dict(fields)
    fields["id"] = run_id
    with tx() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id=:id", fields)


def create_datagen_job(row: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO datagen_jobs (id,owner_user_id,name,mode,status,target_dsn_json,input_json,created_at)"
            " VALUES (:id,:owner_user_id,:name,:mode,:status,:target_dsn_json,:input_json,:created_at)",
            {"owner_user_id": None, **row},
        )


def get_datagen_job(job_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM datagen_jobs WHERE id=?", (job_id,))
    return dict(row) if row else None


def get_datagen_job_for_user(user_id: str, job_id: str) -> Optional[dict]:
    row = _fetch_one(
        "SELECT * FROM datagen_jobs WHERE id=? AND owner_user_id=?",
        (job_id, user_id),
    )
    return dict(row) if row else None


def list_datagen_jobs() -> list:
    rows = _fetch_all("SELECT * FROM datagen_jobs ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def list_datagen_jobs_for_user(user_id: str) -> list:
    rows = _fetch_all(
        "SELECT * FROM datagen_jobs WHERE owner_user_id=? ORDER BY created_at DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def claim_legacy_datagen_jobs(user_id: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE datagen_jobs SET owner_user_id=? WHERE owner_user_id IS NULL",
            (user_id,),
        )
        return cur.rowcount


def update_datagen_job(job_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    fields = dict(fields)
    fields["id"] = job_id
    with tx() as conn:
        conn.execute(f"UPDATE datagen_jobs SET {sets} WHERE id=:id", fields)


def insert_metric(m: dict) -> None:
    cols = [c for c in _METRIC_COLS if c in m]
    with tx() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO metrics (run_id,ts,{','.join(cols)}) VALUES (:run_id,:ts,{':' + ',:'.join(cols)})",
            {**{c: None for c in _METRIC_COLS}, **m},
        )


def get_metrics(run_id: str, ts_from: Optional[int] = None, ts_to: Optional[int] = None) -> list:
    sql = "SELECT * FROM metrics WHERE run_id=?"
    args: list[Any] = [run_id]
    if ts_from is not None:
        sql += " AND ts>=?"
        args.append(ts_from)
    if ts_to is not None:
        sql += " AND ts<=?"
        args.append(ts_to)
    sql += " ORDER BY ts"
    return [dict(r) for r in _fetch_all(sql, tuple(args))]


def save_report(run_id: str, l0: dict, l1: list, md_path: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reports (run_id,l0_json,l1_json,md_path,created_at) VALUES (?,?,?,?,datetime('now'))",
            (run_id, json.dumps(l0, ensure_ascii=False), json.dumps(l1, ensure_ascii=False), md_path),
        )


def get_report(run_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM reports WHERE run_id=?", (run_id,))
    if not row:
        return None
    d = dict(row)
    d["l0"] = json.loads(d.pop("l0_json"))
    # l1_json 有两种形状：新版 dict（带 verdict）、旧版 list[dict]。
    # 归一化后历史报告页不 500，只降级为"旧版规则建议"样式。
    from app.services.l1.contract import normalize_l1
    d["l1"] = normalize_l1(json.loads(d.pop("l1_json")))
    return d
