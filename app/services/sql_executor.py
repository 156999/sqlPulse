import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pymysql

_DELIMITER_RE = re.compile(r"(?i)^DELIMITER\s+(\S+)")


def split_sql_statements(text: str) -> list:
    """按 MySQL 语法拆分 SQL，支持注释、字符串、反引号和 DELIMITER。"""
    stmts: list = []
    buf: list = []
    i, n = 0, len(text)
    delim = ";"
    quote = None
    line_comment = False
    block_comment = False

    while i < n:
        c = text[i]
        if line_comment:
            if c in "\r\n":
                line_comment = False
                buf.append(" ")
            i += 1
            continue
        if block_comment:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                block_comment = False
                buf.append(" ")
                i += 2
            else:
                i += 1
            continue
        if quote:
            buf.append(c)
            if quote == "`":
                if c == "`" and i + 1 < n and text[i + 1] == "`":
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if c == "`":
                    quote = None
            elif c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            line_comment = True
            i += 2
            continue
        if c == "#":
            line_comment = True
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            block_comment = True
            buf.append(" ")
            i += 2
            continue

        if _at_delimiter_command(buf, text, i):
            j = text.find("\n", i)
            if j == -1:
                j = text.find("\r", i)
            if j == -1:
                j = n
            m = _DELIMITER_RE.match(text[i:j].strip())
            if m:
                delim = m.group(1)
                buf = []
                i = j
                if i < n and text[i] == "\r":
                    i += 1
                if i < n and text[i] == "\n":
                    i += 1
                continue

        if c == delim[0] and text.startswith(delim, i):
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += len(delim)
            continue
        buf.append(c)
        i += 1

    s = "".join(buf).strip()
    if s:
        stmts.append(s)
    return stmts


def _at_delimiter_command(buf: list, text: str, i: int) -> bool:
    if text[i : i + 9].upper() != "DELIMITER":
        return False
    if i + 9 < len(text) and not text[i + 9].isspace():
        return False
    return not any(ch and not ch.isspace() for ch in buf)


def _log_line(log_path: Path, line: str) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")


def _one_line(sql: str) -> str:
    compact = " ".join(sql.split())
    if len(compact) > 500:
        return compact[:500] + "..."
    return compact


def execute_sql_job(
    job_id: str,
    dsn: dict,
    statements: list,
    log_path: Path,
    cancel_event: threading.Event,
    timeout_sec: int,
    connect: Callable = pymysql.connect,
) -> tuple:
    if not statements:
        return "failed", None, "没有可执行的 SQL 语句"

    rows_affected = 0
    conn = None
    try:
        conn = connect(
            host=dsn["host"],
            port=dsn["port"],
            user=dsn["user"],
            password=dsn["password"],
            database=dsn["database"],
            connect_timeout=3,
            read_timeout=timeout_sec,
            write_timeout=timeout_sec,
            autocommit=True,
        )
    except Exception as e:
        return "failed", None, f"连接目标库失败：{e}"

    try:
        with conn.cursor() as cur:
            total = len(statements)
            for idx, stmt in enumerate(statements, 1):
                if cancel_event.is_set():
                    return "cancelled", rows_affected, None
                _log_line(log_path, f"[{job_id}] sql {idx}/{total}: {_one_line(stmt)}")
                try:
                    cur.execute(stmt)
                    rc = cur.rowcount
                    as_int = rc if isinstance(rc, int) and rc > 0 else 0
                    rows_affected += as_int
                    _log_line(log_path, f"[{job_id}] sql {idx}/{total} affected {as_int} rows")
                except Exception as e:
                    _log_line(log_path, f"[{job_id}] sql {idx}/{total} failed: {e}")
                    return "failed", rows_affected, f"第 {idx} 条语句执行失败：{e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return "finished", rows_affected, None
