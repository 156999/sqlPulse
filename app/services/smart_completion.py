"""Conservative SQL value completion for the structured pressure-test form."""
import re


TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.I)
TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?"
    r"(?:\s+(?:as\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?",
    re.I,
)
MISSING_VALUE_RE = re.compile(
    r"(?P<prefix>\bwhere\s+|\band\s+|\bor\s+)"
    r"(?P<column>(?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?`?[A-Za-z_][A-Za-z0-9_]*`?)"
    r"(?P<space>\s*)(?P<op>=|<>|!=|>=|<=|>|<)?(?P<tail>\s*)"
    r"(?=(?:\band\b|\bor\b|\border\b|\bgroup\b|\blimit\b|;|$))",
    re.I,
)


def _placeholder(column):
    data_type = (column.get("data_type") or "").lower()
    name = column.get("name", "").lower()
    enum_values = column.get("enum_values") or []
    if enum_values:
        values = ",".join(repr(value) for value in enum_values)
        return f"{{{{pick({values})}}}}", "字段为枚举类型"
    if data_type in {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}:
        return "{{rand(1,10000)}}", "整数类型使用随机整数"
    if data_type in {"decimal", "numeric", "float", "double", "real"}:
        return "{{randf(0,100,2)}}", "小数类型使用随机小数"
    if data_type in {"date"}:
        return "{{randdate('2026-01-01','2026-12-31')}}", "日期类型使用随机日期"
    if data_type in {"datetime", "timestamp"}:
        return "{{randdt('2026-01-01 00:00:00','2026-12-31 23:59:59')}}", "时间类型使用随机日期时间"
    if "uuid" in name:
        return "{{uuid()}}", "字段名提示使用 UUID"
    return "{{randstr(16)}}", "字符串类型使用随机字符串"


def suggest(sql, metadata):
    """Return non-destructive suggestions for a missing WHERE value."""
    table_match = TABLE_RE.search(sql)
    table = table_match.group(1) if table_match else None
    table_metadata = (metadata or {}).get("tables")
    if table_metadata:
        default_metadata = next(iter(table_metadata.values()), {})
    else:
        default_metadata = metadata or {}
    matches = list(MISSING_VALUE_RE.finditer(sql))
    if not matches:
        return {"suggestions": [], "table": table, "message": "未发现缺少比较值的条件"}
    suggestions = []
    for match in matches:
        qualified_name = match.group("column")
        qualifier = qualified_name.rsplit(".", 1)[0].strip("`").lower() if "." in qualified_name else None
        column_name = qualified_name.rsplit(".", 1)[-1].strip("`")
        current_metadata = default_metadata
        if table_metadata and qualifier:
            current_metadata = table_metadata.get(qualifier, default_metadata)
        columns = {c.get("name", "").lower(): c for c in current_metadata.get("columns", [])}
        column = columns.get(column_name.lower())
        if not column:
            continue
        placeholder, reason = _placeholder(column)
        op = match.group("op")
        if op:
            replacement = (
                f"{match.group('prefix')}{match.group('column')}"
                f"{match.group('space')}{op} {placeholder}{match.group('tail')}"
            )
        else:
            # Whitespace after a bare column belongs after the generated value;
            # otherwise a following AND/OR would be swallowed by the replacement.
            replacement = (
                f"{match.group('prefix')}{match.group('column')}"
                f" = {placeholder}{match.group('space')}{match.group('tail')}"
            )
        start, end = match.span()
        suggestions.append({
            "start": start, "end": end, "replacement": replacement,
            "column": column, "placeholder": placeholder, "reason": reason,
            "message": f"为 {column_name} 补充默认直接占位符；如需跨 SQL 复用，请手动改为命名变量。",
        })
    if not suggestions:
        return {"table": table, "suggestions": [], "message": "缺少条件值，但未找到匹配的字段元数据"}
    return {"table": table, "suggestions": suggestions}
