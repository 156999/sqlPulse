"""多表造数的语句分组：按 SQL 的目标表把语句拆成可以并发执行的组。

只做词法识别、不连接数据库。识别不出目标表的语句（SET/BEGIN/COMMIT 等
会话控制语句）统一归入 ``GLOBAL_GROUP``，由调度方先行串行执行。

设计约定：
- 表名大小写按原样保留。MySQL 在 Linux 下表名区分大小写，宁可按原样多分一组，
  也不要把两张不同的表误合并成同一组后并发执行。
- 语句顺序在组内保持不变，因为同表语句之间可能存在先后依赖。
"""

import re
from typing import Optional

GLOBAL_GROUP = "__global__"

_IDENTIFIER = r"`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*"
_TARGET = rf"(?:(?:{_IDENTIFIER})\s*\.\s*)?({_IDENTIFIER})"

_LEADING_COMMENT = re.compile(r"(?is)^\s*(?:(?:--[^\n]*\n)|(?:#[^\n]*\n)|(?:/\*.*?\*/))*")

_TABLE_PATTERNS = (
    re.compile(rf"(?is)^\s*(?:INSERT|REPLACE)\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*INTO\s+{_TARGET}"),
    re.compile(rf"(?is)^\s*UPDATE\s+(?:(?:LOW_PRIORITY|IGNORE)\s+)*{_TARGET}"),
    re.compile(rf"(?is)^\s*DELETE\s+(?:(?:LOW_PRIORITY|QUICK|IGNORE)\s+)*FROM\s+{_TARGET}"),
    re.compile(rf"(?is)^\s*CREATE\s+(?:(?:TEMPORARY|OR\s+REPLACE)\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_TARGET}"),
    re.compile(rf"(?is)^\s*TRUNCATE\s+(?:TABLE\s+)?{_TARGET}"),
    re.compile(rf"(?is)^\s*ALTER\s+(?:IGNORE\s+)?TABLE\s+{_TARGET}"),
    re.compile(rf"(?is)^\s*DROP\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+EXISTS\s+)?{_TARGET}"),
)


def statement_sql(statement) -> str:
    """同时接受原始 SQL 字符串和 compile_statement 产出的编译对象。"""
    if isinstance(statement, str):
        return statement
    return str(getattr(statement, "sql", statement))


def statement_target_table(statement) -> Optional[str]:
    """返回语句写入或修改的目标表名；识别不出时返回 None。"""
    text = _LEADING_COMMENT.sub("", statement_sql(statement))
    for pattern in _TABLE_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group(1).strip("`")
    return None


def group_statements(statements) -> list:
    """按目标表分组，返回 ``[(表名, [语句, ...]), ...]``，组序等于首次出现顺序。"""
    groups: dict = {}
    for statement in statements:
        table = statement_target_table(statement) or GLOBAL_GROUP
        groups.setdefault(table, []).append(statement)
    return list(groups.items())


def describe_statements(statements) -> dict:
    """分组预览用的摘要，供 /api/datagen/analyze 返回给前端。"""
    groups = group_statements(statements)
    return {
        "total_statements": sum(len(items) for _, items in groups),
        "groups": [{"table": table, "statements": len(items)}
                   for table, items in groups if table != GLOBAL_GROUP],
        "global_statements": sum(len(items) for table, items in groups if table == GLOBAL_GROUP),
    }
