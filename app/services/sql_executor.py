import re
import threading
import random
from itertools import product
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pymysql

_DELIMITER_RE = re.compile(r"(?i)^DELIMITER\s+(\S+)")
_SENSITIVE_COLUMN_RE = re.compile(r"(password|passwd|pwd|token|secret|idcard|identity|credential)", re.I)
_INSERT_COLUMNS_RE = re.compile(r"(?is)^\s*INSERT\s+INTO\s+[^()]+\((.*?)\)\s+VALUES\s*\(")
_MAX_DETERMINISTIC_UNIQUE_PROBES = 5000


@dataclass
class UniqueResolution:
    status: str
    args: Optional[tuple] = None
    candidates: Optional[list] = None
    probed: int = 0


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
    if hasattr(sql, "sql"):
        sql = sql.sql
    compact = " ".join(sql.split())
    if len(compact) > 500:
        return compact[:500] + "..."
    return compact


def _diagnostic_params(sql: str, args: tuple) -> tuple:
    match = _INSERT_COLUMNS_RE.search(sql)
    columns = [] if not match else [part.strip().strip("`") for part in match.group(1).split(",")]
    safe = []
    for index, value in enumerate(args):
        if index < len(columns) and _SENSITIVE_COLUMN_RE.search(columns[index]):
            safe.append("******")
        elif isinstance(value, str) and len(value) > 200:
            safe.append(value[:200] + "...")
        else:
            safe.append(value)
    return tuple(safe)


def _diagnostic_sql(cur, sql: str, safe_args: tuple) -> str:
    mogrify = getattr(cur, "mogrify", None)
    if callable(mogrify):
        try:
            return _one_line(mogrify(sql, safe_args))
        except Exception:
            pass
    return f"{_one_line(sql)} | params={safe_args!r}"


def _quote_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"非法标识符：{name}")
    return f"`{name}`"


class SampleCache:
    def __init__(self, conn):
        self.conn = conn
        self.cache = {}

    def warm_from_statements(self, statements: list, variable_definitions: Optional[dict] = None) -> None:
        for stmt in statements:
            for generator in getattr(stmt, "generators", []):
                if getattr(generator, "name", None) == "sample":
                    self._load(*generator.args)
        for generator in (variable_definitions or {}).values():
            if getattr(generator, "name", None) == "sample":
                self._load(*generator.args)

    def sample(self, table: str, column: str, options: dict):
        rows = self._load(table, column, options)
        if not rows:
            raise ValueError(f"sample({table}.{column}) 没有可用样本")
        mode = options.get("mode", "uniform")
        if mode == "weighted":
            values, weights = zip(*rows)
            return random.choices(values, weights=weights)[0]
        return random.choice(rows)[0]

    def _load(self, table: str, column: str, options: dict):
        key = (table, column, tuple(sorted(options.items())))
        if key in self.cache:
            return self.cache[key]
        table_sql = _quote_identifier(table)
        column_sql = _quote_identifier(column)
        where = options.get("where")
        where_sql = f" WHERE {where}" if where else ""
        limit = int(options.get("sample_size", 10000))
        mode = options.get("mode", "uniform")
        params = (limit,)
        if mode == "weighted":
            sql = (
                f"SELECT {column_sql}, COUNT(*) AS c FROM {table_sql}{where_sql} "
                f"GROUP BY {column_sql} ORDER BY c DESC LIMIT %s"
            )
        elif mode == "recent":
            time_column = options.get("time_column")
            if not time_column:
                raise ValueError("sample recent 模式必须指定 time_column")
            sql = f"SELECT {column_sql} FROM {table_sql}{where_sql} ORDER BY {_quote_identifier(time_column)} DESC LIMIT %s"
        else:
            sql = f"SELECT {column_sql} FROM {table_sql}{where_sql} LIMIT %s"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        normalized = []
        for row in rows:
            if isinstance(row, dict):
                values = tuple(row.values())
            else:
                values = tuple(row)
            if mode == "weighted":
                normalized.append((values[0], int(values[1])))
            else:
                normalized.append((values[0], 1))
        self.cache[key] = normalized
        return normalized


class UniqueConstraintGuard:
    def __init__(self, table: Optional[str], indexes: list[dict], max_retries: int = 20):
        self.table = table
        self.indexes = [index for index in indexes if index.get("columns")]
        self.max_retries = max_retries
        self.used = {index["name"]: set() for index in self.indexes}

    @property
    def enabled(self) -> bool:
        return bool(self.table and self.indexes)

    def candidates(self, sql: str, args: tuple) -> list[tuple[dict, tuple]]:
        match = _INSERT_COLUMNS_RE.search(sql)
        if not match:
            return []
        columns = [part.strip().strip("`") for part in match.group(1).split(",")]
        values = dict(zip(columns, args))
        return [
            (index, tuple(values[column] for column in index["columns"]))
            for index in self.indexes if all(column in values for column in index["columns"])
        ]

    def conflict(self, cur, candidates: list[tuple[dict, tuple]]) -> Optional[tuple[str, tuple, str]]:
        for index, key in candidates:
            name = index["name"]
            if key in self.used[name]:
                return name, key, "task"
            where = " AND ".join(f"{_quote_identifier(column)} <=> %s" for column in index["columns"])
            sql = f"SELECT 1 FROM {_quote_identifier(self.table)} WHERE {where} LIMIT 1"
            cur.execute(sql, key)
            if cur.fetchone():
                return name, key, "database"
        return None

    def commit(self, candidates: list[tuple[dict, tuple]]) -> None:
        for index, key in candidates:
            self.used[index["name"]].add(key)


def _insert_columns(sql: str) -> list[str]:
    match = _INSERT_COLUMNS_RE.search(sql)
    if not match:
        return []
    return [part.strip().strip("`") for part in match.group(1).split(",")]


def _resolve_sample_unique_conflict(stmt, sql: str, args: tuple, conflict: tuple[str, tuple, str],
                                    sample_cache: SampleCache, unique_guard: UniqueConstraintGuard, cur,
                                    variable_definitions: Optional[dict] = None):
    if not hasattr(stmt, "generators"):
        return UniqueResolution("not_applicable")
    index_name, _, _ = conflict
    index = next((candidate for candidate in unique_guard.indexes if candidate["name"] == index_name), None)
    if not index or len(index["columns"]) < 2:
        return UniqueResolution("not_applicable")
    columns = _insert_columns(sql)
    generators = list(getattr(stmt, "generators", []))
    if len(columns) != len(args) or len(generators) != len(args):
        return UniqueResolution("not_applicable")

    value_options = []
    column_positions = {}
    for column in index["columns"]:
        try:
            position = columns.index(column)
        except ValueError:
            return UniqueResolution("not_applicable")
        generator = generators[position]
        if getattr(generator, "name", None) == "var" and variable_definitions:
            generator = variable_definitions.get(generator.args[0], generator)
        if getattr(generator, "name", None) != "sample":
            return UniqueResolution("not_applicable")
        rows = sample_cache._load(*generator.args)
        values = [row[0] for row in rows]
        if not values:
            return UniqueResolution("not_applicable")
        random.shuffle(values)
        value_options.append(values)
        column_positions[column] = position

    probes = 0
    for combo in product(*value_options):
        probes += 1
        if probes > _MAX_DETERMINISTIC_UNIQUE_PROBES:
            break
        next_args = list(args)
        for column, value in zip(index["columns"], combo):
            next_args[column_positions[column]] = value
        next_args = tuple(next_args)
        candidates = unique_guard.candidates(sql, next_args)
        if unique_guard.conflict(cur, candidates) is None:
            return UniqueResolution("resolved", next_args, candidates, probes)
    if probes <= _MAX_DETERMINISTIC_UNIQUE_PROBES:
        return UniqueResolution("exhausted", probed=probes)
    return UniqueResolution("not_applicable", probed=probes)


def execute_sql_job(
    job_id: str,
    dsn: dict,
    statements: list,
    log_path: Path,
    cancel_event: threading.Event,
    timeout_sec: int,
    connect: Callable = pymysql.connect,
    row_count: int = 1,
    variable_definitions: Optional[dict] = None,
    target_table: Optional[str] = None,
    unique_indexes: Optional[list[dict]] = None,
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
        sample_cache = SampleCache(conn)
        unique_guard = UniqueConstraintGuard(target_table, unique_indexes or [])
        try:
            sample_cache.warm_from_statements(statements, variable_definitions)
        except Exception as e:
            return "failed", rows_affected, f"采样缓存初始化失败：{e}"
        with conn.cursor() as cur:
            total = len(statements) * row_count
            idx = 0
            statement_count = len(statements)
            for row_index in range(1, row_count + 1):
              for statement_index, stmt in enumerate(statements, 1):
                idx += 1
                if cancel_event.is_set():
                    return "cancelled", rows_affected, None
                location = (
                    f"execution {idx}/{total} "
                    f"(generated-row {row_index}/{row_count}, statement {statement_index}/{statement_count})"
                )
                query, args = (stmt, ())
                try:
                    candidates = []
                    for attempt in range(unique_guard.max_retries + 1):
                        if hasattr(stmt, "bind"):
                            values = {"__sample__": sample_cache.sample}
                            values.update({
                                name: generator.sample(values)
                                for name, generator in (variable_definitions or {}).items()
                            })
                            query, args = stmt.bind(values)
                        candidates = unique_guard.candidates(str(query), tuple(args or ()))
                        conflict = unique_guard.conflict(cur, candidates) if unique_guard.enabled else None
                        if not conflict:
                            break
                        resolution = _resolve_sample_unique_conflict(
                            stmt, str(query), tuple(args or ()), conflict, sample_cache, unique_guard, cur,
                            variable_definitions,
                        )
                        if resolution.status == "resolved":
                            args, candidates = resolution.args, resolution.candidates
                            break
                        index_name, key, source = conflict
                        if resolution.status == "exhausted":
                            _log_line(
                                log_path,
                                f"[{job_id}] {location} unique conflict index={index_name} "
                                f"sample combination space exhausted probed={resolution.probed}",
                            )
                            raise ValueError(
                                f"唯一约束 {index_name} 的 sample 候选组合已全部冲突，"
                                f"请扩大父表样本范围或清理目标表已有组合"
                            )
                        _log_line(
                            log_path,
                            f"[{job_id}] {location} unique conflict index={index_name} "
                            f"candidate={key!r} source={source} retry={attempt + 1}/{unique_guard.max_retries}",
                        )
                    else:
                        raise ValueError(f"唯一约束候选组合在 {unique_guard.max_retries} 次重试后仍冲突")
                    _log_line(log_path, f"[{job_id}] {location}: {_one_line(query)}")
                    if args:
                        cur.execute(query, args)
                    else:
                        cur.execute(query)
                    rc = cur.rowcount
                    as_int = rc if isinstance(rc, int) and rc > 0 else 0
                    rows_affected += as_int
                    unique_guard.commit(candidates)
                    _log_line(log_path, f"[{job_id}] {location} affected {as_int} rows")
                except Exception as e:
                    safe_args = _diagnostic_params(str(query), tuple(args or ()))
                    error_code = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
                    _log_line(log_path, f"[{job_id}] {location} failed: mysql_error={error_code or '-'} message={e}")
                    _log_line(log_path, f"[{job_id}] failed SQL: {_diagnostic_sql(cur, str(query), safe_args)}")
                    _log_line(log_path, f"[{job_id}] failed params: {safe_args!r}")
                    return "failed", rows_affected, f"第 {idx} 次执行失败（生成行 {row_index}，SQL {statement_index}）：{e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return "finished", rows_affected, None
