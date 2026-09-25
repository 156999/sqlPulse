import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pymysql

SENSITIVE_COLUMN_RE = re.compile(r"(password|passwd|pwd|token|secret|idcard|identity|credential)", re.I)

INTEGER_RANGES = {
    "tinyint": (-128, 127), "smallint": (-32768, 32767),
    "mediumint": (-8388608, 8388607), "int": (-2147483648, 2147483647),
    "integer": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
}


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    column_type: str
    nullable: bool
    default: Any = None
    max_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    extra: str = ""
    key: str = ""
    enum_values: list[str] = field(default_factory=list)
    sensitive: bool = False


def list_table_names(dsn: dict, query: str = "", connect=pymysql.connect) -> list[str]:
    conn = connect(
        host=dsn["host"], port=dsn["port"], user=dsn["user"], password=dsn["password"],
        database=dsn["database"], connect_timeout=3, autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'"
            )
            params = [dsn["database"]]
            if query.strip():
                sql += " AND TABLE_NAME LIKE %s"
                params.append(f"%{query.strip()}%")
            sql += " ORDER BY TABLE_NAME LIMIT 500"
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [str(row[0] if not isinstance(row, dict) else row["TABLE_NAME"]) for row in rows]


def read_table_metadata(dsn: dict, table: str, connect=pymysql.connect) -> dict:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("表名必须为安全标识符")
    conn = connect(
        host=dsn["host"],
        port=dsn["port"],
        user=dsn["user"],
        password=dsn["password"],
        database=dsn["database"],
        connect_timeout=3,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
                       EXTRA, COLUMN_KEY
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                ORDER BY ORDINAL_POSITION
                """,
                (dsn["database"], table),
            )
            columns = [_column_from_row(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT s.INDEX_NAME, s.NON_UNIQUE, s.COLUMN_NAME, s.SEQ_IN_INDEX
                FROM information_schema.STATISTICS s
                WHERE s.TABLE_SCHEMA=%s AND s.TABLE_NAME=%s
                ORDER BY s.INDEX_NAME, s.SEQ_IN_INDEX
                """,
                (dsn["database"], table),
            )
            unique_indexes = _group_unique_indexes(cur.fetchall())
            cur.execute(
                """
                SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL
                """,
                (dsn["database"], table),
            )
            foreign_keys = list(cur.fetchall())
            cur.execute(
                """
                SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
                FROM information_schema.TABLE_CONSTRAINTS tc
                JOIN information_schema.CHECK_CONSTRAINTS cc
                  ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA
                 AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME
                WHERE tc.CONSTRAINT_SCHEMA=%s AND tc.TABLE_NAME=%s
                  AND tc.CONSTRAINT_TYPE='CHECK'
                """,
                (dsn["database"], table),
            )
            checks = list(cur.fetchall())
    finally:
        conn.close()
    return {
        "table": table,
        "columns": [c.__dict__ for c in columns],
        "unique_indexes": unique_indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
        "unrecognized": ["复杂 CHECK 约束、生成列表达式和自动更新时间需人工确认"],
    }


def _column_from_row(row: dict) -> ColumnMeta:
    column_type = row["COLUMN_TYPE"] or row["DATA_TYPE"]
    enum_values = []
    if row["DATA_TYPE"] in ("enum", "set"):
        enum_values = re.findall(r"'((?:[^'\\]|\\.)*)'", column_type)
    return ColumnMeta(
        name=row["COLUMN_NAME"],
        data_type=row["DATA_TYPE"],
        column_type=column_type,
        nullable=row["IS_NULLABLE"] == "YES",
        default=row["COLUMN_DEFAULT"],
        max_length=row["CHARACTER_MAXIMUM_LENGTH"],
        numeric_precision=row["NUMERIC_PRECISION"],
        numeric_scale=row["NUMERIC_SCALE"],
        extra=row["EXTRA"] or "",
        key=row["COLUMN_KEY"] or "",
        enum_values=enum_values,
        sensitive=bool(SENSITIVE_COLUMN_RE.search(row["COLUMN_NAME"])),
    )


def _group_unique_indexes(rows: list[dict]) -> list[dict]:
    grouped = {}
    for row in rows:
        if int(row["NON_UNIQUE"]) != 0:
            continue
        grouped.setdefault(row["INDEX_NAME"], []).append(row["COLUMN_NAME"])
    return [{"name": name, "columns": columns} for name, columns in grouped.items()]


def validate_field_rules(metadata: dict, rules: list[dict], row_count: int = 1) -> list[dict]:
    columns = {col["name"]: col for col in metadata.get("columns", [])}
    errors = []
    for rule in rules:
        column_name = rule.get("column")
        column = columns.get(column_name)
        if not column:
            errors.append({"column": column_name, "message": "字段不存在于目标表"})
            continue
        if column.get("extra") and ("auto_increment" in column["extra"] or "VIRTUAL" in column["extra"].upper()):
            if not rule.get("override"):
                continue
        nullable = rule.get("nullable", column.get("nullable", True))
        null_ratio = Decimal(str(rule.get("null_ratio", 0) or 0))
        if not nullable and null_ratio > 0:
            errors.append({"column": column_name, "message": "NOT NULL 字段不能配置空值比例"})
        length = rule.get("length") or {}
        max_length = column.get("max_length")
        if max_length is not None and length.get("max") is not None and int(length["max"]) > int(max_length):
            errors.append({"column": column_name, "message": f"长度上限不能超过数据库限制 {max_length}"})
        enum_values = column.get("enum_values") or []
        candidates = rule.get("values") or []
        if enum_values and candidates:
            invalid = [v for v in candidates if v not in enum_values]
            if invalid:
                errors.append({"column": column_name, "message": f"枚举值不在数据库允许范围内：{invalid}"})
        if rule.get("unique") and candidates and row_count > len(set(candidates)):
            errors.append({"column": column_name, "message": "生成条数超过可用唯一值空间"})
    unique_indexes = metadata.get("unique_indexes") or []
    configured = {rule.get("column") for rule in rules if rule.get("unique") or rule.get("generator")}
    for index in unique_indexes:
        missing = [col for col in index["columns"] if col not in configured]
        if len(index["columns"]) > 1 and missing:
            errors.append({"column": ",".join(index["columns"]), "message": "联合唯一索引缺少完整字段规则"})
    return errors


def apply_database_rules(metadata: dict, rules: list[dict], row_count: int = 1) -> dict:
    columns = {col["name"]: col for col in metadata.get("columns", [])}
    unique_columns = {
        index["columns"][0] for index in metadata.get("unique_indexes", [])
        if len(index.get("columns", [])) == 1
    }
    foreign_keys = {fk["COLUMN_NAME"]: fk for fk in metadata.get("foreign_keys", [])}
    derived_rules = _derived_check_rules(metadata)
    normalized, changes, errors, warnings = [], [], [], []

    def change(column, path, before, after, reason):
        if before != after:
            changes.append({"column": column, "path": path, "before": before, "after": after, "reason": reason})

    for source in rules:
        rule = deepcopy(source)
        name = rule.get("column")
        column = columns.get(name)
        if not column:
            errors.append({"column": name, "message": "字段不存在于目标表"})
            continue
        rule.setdefault("params", {})
        rule["nullable"] = bool(column.get("nullable"))
        rule["unique"] = name in unique_columns or column.get("key") in ("PRI", "UNI")
        extra = str(column.get("extra") or "").lower()
        if "auto_increment" in extra or "generated" in extra:
            before = rule.get("generator")
            rule["generator"] = "omit"
            change(name, "generator", before, "omit", "自增列或生成列由数据库维护")
        elif column.get("default") is not None and not rule.get("override"):
            before = rule.get("generator")
            rule["generator"] = "omit"
            change(name, "generator", before, "omit", "字段存在默认值")

        if not column.get("nullable", True) and float(rule.get("null_ratio") or 0) != 0:
            before = rule.get("null_ratio")
            rule["null_ratio"] = 0
            change(name, "null_ratio", before, 0, "NOT NULL 字段不能生成空值")

        generator, params = rule.get("generator"), rule["params"]
        data_type = str(column.get("data_type") or "").lower()
        column_type = str(column.get("column_type") or "").lower()
        if name in derived_rules and rule.get("generator") != "omit":
            derived = derived_rules[name]
            before = rule.get("generator")
            rule["generator"] = "derived"
            rule["derived_expression"] = derived["expression"]
            rule["dependencies"] = derived["dependencies"]
            change(name, "generator", before, "derived", f"应用 CHECK {derived['constraint']}")
            generator = "derived"
        if generator in ("rand", "sample") and data_type in INTEGER_RANGES:
            low, high = INTEGER_RANGES[data_type]
            if "unsigned" in column_type:
                low, high = 0, high * 2 + 1
            before_min, before_max = params.get("min", low), params.get("max", high)
            params["min"], params["max"] = max(int(before_min), low), min(int(before_max), high)
            change(name, "params.min", before_min, params["min"], f"受 {column_type} 类型范围限制")
            change(name, "params.max", before_max, params["max"], f"受 {column_type} 类型范围限制")
            if params["min"] > params["max"]:
                errors.append({"column": name, "message": "用户整数范围与数据库范围没有交集"})
        elif generator in ("randf", "sample") and data_type in ("decimal", "numeric"):
            precision = int(column.get("numeric_precision") or 10)
            scale = int(column.get("numeric_scale") or 0)
            limit = Decimal(10) ** (precision - scale) - Decimal(10) ** (-scale)
            before_digits = params.get("digits", scale)
            params["digits"] = min(int(before_digits), scale)
            change(name, "params.digits", before_digits, params["digits"], f"受 {column_type} 小数位限制")
            for key, bound in (("min", -limit), ("max", limit)):
                before = params.get(key, str(bound))
                value = max(Decimal(str(before)), bound) if key == "min" else min(Decimal(str(before)), bound)
                params[key] = str(value)
                change(name, f"params.{key}", before, params[key], f"受 {column_type} 精度限制")
        elif generator in ("randstr", "sample") and column.get("max_length") is not None:
            limit = int(column["max_length"])
            before = params.get("max_length", params.get("length", limit))
            params["max_length"] = min(int(before), limit)
            params.pop("length", None)
            change(name, "params.max_length", before, params["max_length"], f"受 {column_type} 长度限制")

        enum_values = column.get("enum_values") or []
        if enum_values and generator in ("pick", "sample"):
            before = params.get("values") or enum_values
            params["values"] = [value for value in before if value in enum_values]
            change(name, "params.values", before, params["values"], "移除数据库枚举范围之外的候选值")
            if not params["values"]:
                errors.append({"column": name, "message": "枚举候选值与数据库允许值没有交集"})

        if name in foreign_keys and generator != "omit":
            fk = foreign_keys[name]
            before = generator
            rule["generator"] = "sample"
            before_ratio = rule.get("sample_ratio", 100)
            rule["sample_ratio"] = 100
            rule["sample_source"] = {"table": fk["REFERENCED_TABLE_NAME"], "column": fk["REFERENCED_COLUMN_NAME"]}
            change(name, "generator", before, "sample", "外键字段从被引用字段抽样")
            change(name, "sample_ratio", before_ratio, 100, "外键字段必须使用真实父表键值")
        else:
            rule.setdefault("sample_source", {"table": metadata.get("table"), "column": name})

        if name in unique_columns and name not in foreign_keys and rule.get("generator") == "sample":
            before = rule["generator"]
            fallback = rule.get("base_generator")
            if fallback not in ("rand", "randf", "randstr", "pick", "randdate", "randdt", "uuid"):
                fallback = "rand" if data_type in INTEGER_RANGES else "randf" if data_type in ("decimal", "numeric", "float", "double") else "randstr"
            rule["generator"] = fallback
            rule["sample_ratio"] = 0
            change(name, "generator", before, fallback, "主键或唯一字段不能从已有数据重复抽样")
            change(name, "sample_ratio", source.get("sample_ratio", 100), 0, "唯一字段禁用 sample")
        rule["status"] = "adjusted" if any(item["column"] == name for item in changes) else "passed"
        normalized.append(rule)

    coalesced = {}
    for item in changes:
        key = (item["column"], item["path"])
        if key not in coalesced:
            coalesced[key] = dict(item)
        else:
            coalesced[key]["after"] = item["after"]
            coalesced[key]["reason"] = item["reason"]

    def equivalent(left, right):
        if left == right:
            return True
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except Exception:
            return False

    changes = [item for item in coalesced.values() if not equivalent(item["before"], item["after"])]
    for rule in normalized:
        rule["status"] = "adjusted" if any(item["column"] == rule["column"] for item in changes) else "passed"
    errors.extend(validate_field_rules(metadata, normalized, row_count))
    return {"ok": not errors, "rules": normalized, "changes": changes, "errors": errors, "warnings": warnings}


def _derived_check_rules(metadata: dict) -> dict:
    column_names = {column["name"] for column in metadata.get("columns", [])}
    result = {}
    for check in metadata.get("checks") or []:
        clause = str(check.get("CHECK_CLAUSE") or "").replace("`", "")
        if not re.fullmatch(r"[A-Za-z0-9_+\-*/.()=\s]+", clause):
            continue
        for target in column_names:
            match = re.search(rf"\b{re.escape(target)}\b\s*=\s*(.+)", clause)
            if not match:
                continue
            expression = match.group(1).strip()
            while expression.startswith("(") and expression.endswith(")"):
                expression = expression[1:-1].strip()
            dependencies = sorted({name for name in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expression) if name in column_names})
            if dependencies and target not in dependencies:
                result[target] = {
                    "expression": expression,
                    "dependencies": dependencies,
                    "constraint": check.get("CONSTRAINT_NAME") or "CHECK",
                }
                break
    return result
