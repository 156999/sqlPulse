"""目标库元数据只读读取：列定义 / 全部索引 / 表规模估算。

只读边界：
- 只查 information_schema（3 条固定 SELECT），不读业务数据行、不执行 COUNT/DISTINCT、
  不建索引、不写任何东西（连接 autocommit，语句只有 SELECT）。
- 不用 ``SHOW INDEX``：它无法参数化；information_schema 可以全部 ``%s`` 传参。
- 表名先过白名单正则，不做任何拼接。
- DSN 由调用方从 run 快照解析后传入；本模块不读平台配置、不落盘、不缓存 DSN，
  错误文本里的已知密码会被替换掉。

开关：环境变量 ``AI_DB_PROBE_ENABLED``（默认关闭）。关闭时调用方不应连库。
"""
import os
import re
from datetime import datetime
from typing import Optional

import pymysql

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 5
MAX_COLUMNS = 200
MAX_INDEXES = 100

_ENV_FLAG = "AI_DB_PROBE_ENABLED"
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CRED_RE = re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*[^\s,;]+")

CAVEATS = [
    "元数据是**此刻**从目标库读的；压测时的库结构与统计信息可能已经不同（尤其压测含写入时）。",
    "TABLE_ROWS 与 CARDINALITY 都是估算值（InnoDB 采样得到），误差可达数十个百分点，"
    "不要据此推断具体行数或选择率。",
    "本工具只读元数据：没有读取任何业务数据行，没有 COUNT/DISTINCT，没有任何写操作。",
    "有索引不等于会被使用、没有索引也不等于必然全表扫描；是否走索引看执行计划与优化器选择。",
    "索引名/列名按目标库原样返回；表名大小写在区分大小写的目标库上有意义。",
]

_COLUMNS_SQL = """
SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
       EXTRA, COLUMN_KEY, ORDINAL_POSITION
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
ORDER BY ORDINAL_POSITION
"""

_STATISTICS_SQL = """
SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART,
       INDEX_TYPE, CARDINALITY, COLLATION, NULLABLE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""

_TABLES_SQL = """
SELECT ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, TABLE_COLLATION,
       ROW_FORMAT, TABLE_TYPE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
"""


class DbProfileError(Exception):
    """预期错误：code/message/retryable，由工具层映射成统一错误对象。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def enabled() -> bool:
    """AI 是否允许读取目标库元数据；默认关闭，需显式开启。"""
    raw = (os.environ.get(_ENV_FLAG) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def validate_table_name(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise DbProfileError("INVALID_TABLE_NAME", "table_name 必须是非空字符串")
    value = name.strip()
    if not _TABLE_RE.fullmatch(value):
        raise DbProfileError(
            "INVALID_TABLE_NAME",
            "table_name 只允许字母、数字、下划线，且以字母或下划线开头",
        )
    return value


def _redact(text, password: Optional[str]) -> str:
    out = text if isinstance(text, str) else str(text)
    if password:
        out = out.replace(password, "******")
    return _CRED_RE.sub(lambda m: f"{m.group(1)}=***", out)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _map_error(exc: Exception, dsn: dict) -> DbProfileError:
    """把 pymysql 异常映射成稳定错误码；文本里的密码先替换掉。"""
    errno = exc.args[0] if isinstance(exc, pymysql.err.MySQLError) and exc.args else None
    message = _redact(f"{type(exc).__name__}: {exc}", dsn.get("password"))
    if errno in (2002, 2003, 2004, 2005, 1049):
        return DbProfileError("DB_CONNECT_FAILED", f"无法连接目标库：{message}", True)
    if errno in (2006, 2013):
        return DbProfileError("DB_TIMEOUT", f"目标库连接中断/超时：{message}", True)
    if errno in (1044, 1045, 1142, 1143, 1227):
        return DbProfileError("DB_PERMISSION_DENIED", f"目标库权限不足：{message}", False)
    if isinstance(exc, pymysql.err.MySQLError):
        return DbProfileError("DB_QUERY_FAILED", f"读取元数据失败：{message}", False)
    return DbProfileError("DB_CONNECT_FAILED", f"连接目标库异常：{message}", True)


def read_table_profile(dsn: dict, table: str, *, connect=pymysql.connect) -> dict:
    """读一张表的列、全部索引与规模估算。连接工厂可注入，便于离线替身验证。

    失败抛 :class:`DbProfileError`；目标表不存在（information_schema 无该表的列）时
    抛 ``TABLE_NOT_FOUND``。
    """
    table = validate_table_name(table)
    database = dsn.get("database")
    try:
        conn = connect(
            host=dsn.get("host"), port=int(dsn.get("port")), user=dsn.get("user"),
            password=dsn.get("password"), database=database,
            connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
            autocommit=True, cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:  # noqa: BLE001 连接失败是一等公民：统一成错误码
        raise _map_error(exc, dsn) from exc

    missing: list = []
    try:
        with conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (database, table))
            raw_columns = cur.fetchall() or []
            cur.execute(_STATISTICS_SQL, (database, table))
            raw_indexes = cur.fetchall() or []
            cur.execute(_TABLES_SQL, (database, table))
            raw_table = cur.fetchone() or {}
    except Exception as exc:  # noqa: BLE001 同上
        raise _map_error(exc, dsn) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not raw_columns:
        raise DbProfileError(
            "TABLE_NOT_FOUND",
            f"目标库 {database} 中没有表 {table}（或当前账号看不到它）",
        )

    columns = []
    for row in raw_columns[:MAX_COLUMNS]:
        columns.append({
            "name": row.get("COLUMN_NAME"),
            "column_type": row.get("COLUMN_TYPE") or row.get("DATA_TYPE"),
            "data_type": row.get("DATA_TYPE"),
            "nullable": row.get("IS_NULLABLE") == "YES",
            "default": row.get("COLUMN_DEFAULT"),
            "key": row.get("COLUMN_KEY") or "",
            "extra": row.get("EXTRA") or "",
            "max_length": _int_or_none(row.get("CHARACTER_MAXIMUM_LENGTH")),
            "numeric_precision": _int_or_none(row.get("NUMERIC_PRECISION")),
            "numeric_scale": _int_or_none(row.get("NUMERIC_SCALE")),
        })
    if len(raw_columns) > MAX_COLUMNS:
        missing.append(f"columns 超过 {MAX_COLUMNS} 条，已截断（共 {len(raw_columns)} 列）")

    indexes: dict = {}
    for row in raw_indexes:
        name = row.get("INDEX_NAME")
        entry = indexes.setdefault(name, {
            "name": name,
            "unique": row.get("NON_UNIQUE") == 0,
            "type": row.get("INDEX_TYPE"),
            "cardinality": _int_or_none(row.get("CARDINALITY")),
            "columns": [],
        })
        entry["columns"].append({
            "name": row.get("COLUMN_NAME"),
            "seq": _int_or_none(row.get("SEQ_IN_INDEX")),
            "sub_part": _int_or_none(row.get("SUB_PART")),
            "collation": row.get("COLLATION"),
        })
    index_list = list(indexes.values())[:MAX_INDEXES]
    if len(indexes) > MAX_INDEXES:
        missing.append(f"indexes 超过 {MAX_INDEXES} 条，已截断（共 {len(indexes)} 个索引）")
    if not index_list:
        missing.append("indexes 为空：该表没有任何索引")

    table_stats = {
        "engine": raw_table.get("ENGINE"),
        "table_type": raw_table.get("TABLE_TYPE"),
        "table_rows": _int_or_none(raw_table.get("TABLE_ROWS")),
        "data_length": _int_or_none(raw_table.get("DATA_LENGTH")),
        "index_length": _int_or_none(raw_table.get("INDEX_LENGTH")),
        "table_collation": raw_table.get("TABLE_COLLATION"),
        "row_format": raw_table.get("ROW_FORMAT"),
        "estimated": True,  # TABLE_ROWS / DATA_LENGTH 都是估算
    }
    if not raw_table:
        missing.append("table_stats 为空：information_schema.TABLES 中没有该表（视图/临时表或权限受限）")
    elif raw_table.get("TABLE_TYPE") and raw_table["TABLE_TYPE"] != "BASE TABLE":
        missing.append(f"该对象不是基表（TABLE_TYPE={raw_table['TABLE_TYPE']}），规模与索引含义有限")

    return {
        "table": table,
        "database": database,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "source": "information_schema（实时读取目标库，不是压测时快照）",
        "columns": columns,
        "indexes": index_list,
        "table_stats": table_stats,
        "missing": missing,
        "caveats": list(CAVEATS),
    }
