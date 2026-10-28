import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional
from uuid import uuid4

from app.config import settings
from app.services.credential_crypto import credential_crypto, encrypt_dsn_snapshot

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
  process_exit_code INTEGER,
  final_metrics_json TEXT,
  observability_json TEXT,
  finalized_at TEXT,
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
  max_connections INTEGER,
  active_users INTEGER,
  connection_usage_pct REAL,
  mysql_qps     REAL,
  slow_inc      REAL,
  lock_waits_inc REAL,
  lock_wait_ms_inc REAL,
  lock_current_waits INTEGER,
  deadlocks_inc REAL,
  innodb_log_waits_inc REAL,
  innodb_log_writes_inc REAL,
  innodb_log_write_requests_inc REAL,
  innodb_log_pending_fsyncs INTEGER,
  innodb_log_pending_writes INTEGER,
  tmp_disk_inc  REAL,
  bufpool_hit   REAL,
  cpu_pct REAL,
  memory_pct REAL,
  memory_used_mb REAL,
  disk_used_pct REAL,
  disk_read_mb_s REAL,
  disk_write_mb_s REAL,
  net_recv_mb_s REAL,
  net_sent_mb_s REAL,
  metric_scope  TEXT NOT NULL DEFAULT 'window',
  request_count INTEGER,
  request_failures INTEGER,
  exception_count INTEGER,
  user_start_failures INTEGER,
  failure_events INTEGER,
  top_exception_message TEXT,
  top_exception_count INTEGER,
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
  password_ciphertext TEXT,
  database    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_mysql_connections_user
  ON mysql_connections (user_id);
CREATE TABLE IF NOT EXISTS ai_keys (
  user_id        TEXT PRIMARY KEY,
  key_encrypted  TEXT NOT NULL,
  masked         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_jobs (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL,
  user_id       TEXT NOT NULL,
  status        TEXT NOT NULL,
  result_json   TEXT,
  error_code    TEXT,
  error_msg     TEXT,
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_run
  ON ai_jobs (run_id, created_at);
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
    "threads_running", "threads_connected", "max_connections", "active_users", "connection_usage_pct",
    "mysql_qps", "slow_inc", "lock_waits_inc", "lock_wait_ms_inc", "lock_current_waits",
    "deadlocks_inc", "innodb_log_waits_inc", "innodb_log_writes_inc", "innodb_log_write_requests_inc",
    "innodb_log_pending_fsyncs", "innodb_log_pending_writes", "tmp_disk_inc", "bufpool_hit",
    "cpu_pct", "memory_pct", "memory_used_mb", "disk_used_pct", "disk_read_mb_s", "disk_write_mb_s",
    "net_recv_mb_s", "net_sent_mb_s",
    "metric_scope", "request_count", "request_failures", "exception_count",
    "user_start_failures", "failure_events", "top_exception_message", "top_exception_count",
]
# 旧库升级：缺列则补
_METRIC_ALTERS = {
    "max_connections": "INTEGER", "active_users": "INTEGER", "connection_usage_pct": "REAL",
    "mysql_qps": "REAL", "slow_inc": "REAL", "lock_waits_inc": "REAL", "lock_wait_ms_inc": "REAL",
    "lock_current_waits": "INTEGER", "deadlocks_inc": "REAL", "innodb_log_waits_inc": "REAL",
    "innodb_log_writes_inc": "REAL", "innodb_log_write_requests_inc": "REAL",
    "innodb_log_pending_fsyncs": "INTEGER", "innodb_log_pending_writes": "INTEGER",
    "tmp_disk_inc": "REAL", "bufpool_hit": "REAL",
    "cpu_pct": "REAL", "memory_pct": "REAL", "memory_used_mb": "REAL", "disk_used_pct": "REAL",
    "disk_read_mb_s": "REAL", "disk_write_mb_s": "REAL", "net_recv_mb_s": "REAL", "net_sent_mb_s": "REAL",
    "metric_scope": "TEXT NOT NULL DEFAULT 'window'",
    "request_count": "INTEGER", "request_failures": "INTEGER",
    "exception_count": "INTEGER", "user_start_failures": "INTEGER",
    "failure_events": "INTEGER", "top_exception_message": "TEXT",
    "top_exception_count": "INTEGER",
}

# These columns were used by the removed collection-quality view.  Older
# SQLite databases may still contain them; keep them out of the API surface.
_REMOVED_METRIC_COLS = {
    "source_ts", "sample_age_ms", "stale", "collection_error", "sample_interval_s",
}


def init_schema() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        run_columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        if "groups_json" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN groups_json TEXT NOT NULL DEFAULT '[]'")
        if "variables_json" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN variables_json TEXT NOT NULL DEFAULT '{}'")
        for col, typ in {
            "process_exit_code": "INTEGER",
            "final_metrics_json": "TEXT",
            "observability_json": "TEXT",
            "finalized_at": "TEXT",
        }.items():
            if col not in run_columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
        connection_columns = {r[1] for r in conn.execute("PRAGMA table_info(mysql_connections)")}
        if "password_ciphertext" not in connection_columns:
            conn.execute("ALTER TABLE mysql_connections ADD COLUMN password_ciphertext TEXT")
        existing = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
        for col, typ in _METRIC_ALTERS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")
        datagen_columns = {r[1] for r in conn.execute("PRAGMA table_info(datagen_jobs)")}
        if "owner_user_id" not in datagen_columns:
            conn.execute("ALTER TABLE datagen_jobs ADD COLUMN owner_user_id TEXT")
        _migrate_plaintext_credentials(conn)
    seed_root_user()


def _migrate_plaintext_credentials(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, password, password_ciphertext FROM mysql_connections"
    ).fetchall()
    for row in rows:
        if row["password_ciphertext"] or not row["password"]:
            continue
        conn.execute(
            "UPDATE mysql_connections SET password='', password_ciphertext=? WHERE id=?",
            (credential_crypto.encrypt(row["password"]), row["id"]),
        )

    for table, column in (("runs", "db_dsn_json"), ("datagen_jobs", "target_dsn_json")):
        rows = conn.execute(f"SELECT id, {column} FROM {table}").fetchall()
        for row in rows:
            try:
                dsn = json.loads(row[column])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{table}.{column} contains invalid JSON for id={row['id']}") from exc
            if "password" not in dsn:
                continue
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE id=?",
                (json.dumps(encrypt_dsn_snapshot(dsn), ensure_ascii=False), row["id"]),
            )


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
        if "password_ciphertext" not in params:
            params["password_ciphertext"] = credential_crypto.encrypt(params.pop("password", ""))
        params["password"] = ""
        c.execute(
            "INSERT INTO mysql_connections"
            " (id,user_id,name,host,port,user,password,password_ciphertext,database,created_at,updated_at)"
            " VALUES (:id,:user_id,:name,:host,:port,:user,:password,:password_ciphertext,:database,:created_at,:updated_at)",
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
        if "password" in fields and "password_ciphertext" not in fields:
            fields["password_ciphertext"] = credential_crypto.encrypt(fields.pop("password"))
            fields["password"] = ""
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


# ---- AI 诊断：用户自填模型 key（加密落库）与后台诊断 job ----
# ai_keys 存 Fernet 密文（密钥由 APP_SECRET_KEY 派生，见 app/ai/key_store.py），
# 明文只出现在 key_store 的加解密瞬间；masked 仅供页面回显。
# ai_jobs 复刻 runs/datagen_jobs 的状态机：pending → running → finished/failed，
# 平台重启时由 recover_ai_orphans() 统一标记失败。

def upsert_ai_key(user_id: str, key_encrypted: str, masked: str) -> Optional[dict]:
    now = datetime.now().isoformat(timespec="seconds")
    with tx() as conn:
        conn.execute(
            "INSERT INTO ai_keys (user_id,key_encrypted,masked,created_at,updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " key_encrypted=excluded.key_encrypted, masked=excluded.masked,"
            " updated_at=excluded.updated_at",
            (user_id, key_encrypted, masked, now, now),
        )
    return get_ai_key(user_id)


def get_ai_key(user_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM ai_keys WHERE user_id=?", (user_id,))
    return dict(row) if row else None


def delete_ai_key(user_id: str) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM ai_keys WHERE user_id=?", (user_id,))
        return cur.rowcount > 0


def create_ai_job(job_id: str, run_id: str, user_id: str, status: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO ai_jobs (id,run_id,user_id,status,created_at) VALUES (?,?,?,?,?)",
            (job_id, run_id, user_id, status, datetime.now().isoformat(timespec="seconds")),
        )


def get_ai_job(job_id: str) -> Optional[dict]:
    row = _fetch_one("SELECT * FROM ai_jobs WHERE id=?", (job_id,))
    return dict(row) if row else None


def get_latest_ai_job(run_id: str) -> Optional[dict]:
    row = _fetch_one(
        "SELECT * FROM ai_jobs WHERE run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (run_id,),
    )
    return dict(row) if row else None


def update_ai_job(job_id: str, fields: dict) -> None:
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    fields = dict(fields)
    fields["id"] = job_id
    with tx() as conn:
        conn.execute(f"UPDATE ai_jobs SET {sets} WHERE id=:id", fields)


def list_inflight_ai_jobs(run_id: str) -> list:
    rows = _fetch_all(
        "SELECT * FROM ai_jobs WHERE run_id=? AND status IN ('pending','running')",
        (run_id,),
    )
    return [dict(r) for r in rows]


def count_inflight_ai_jobs_by_user(user_id: str) -> int:
    row = _fetch_one(
        "SELECT COUNT(*) AS c FROM ai_jobs WHERE user_id=? AND status IN ('pending','running')",
        (user_id,),
    )
    return int(row["c"]) if row else 0


def recover_ai_orphans() -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE ai_jobs SET status='failed', error_code='PLATFORM_RESTARTED',"
            " error_msg='平台重启中断', ended_at=datetime('now')"
            " WHERE status IN ('pending','running')"
        )
        return cur.rowcount


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
    points = [dict(r) for r in _fetch_all(sql, tuple(args))]
    for point in points:
        for col in _REMOVED_METRIC_COLS:
            point.pop(col, None)
    return points


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
