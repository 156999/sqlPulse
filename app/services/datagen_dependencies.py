import math
import re
from collections import defaultdict, deque

import pymysql

from app.services.datagen_rules import read_table_metadata
from app.services.datagen_rules import INTEGER_RANGES

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value or ""):
        raise ValueError(f"不支持的表或字段名：{value}")
    return f"`{value}`"


def _table_count(dsn: dict, table: str, column: str, connect) -> int:
    conn = connect(host=dsn["host"], port=dsn["port"], user=dsn["user"], password=dsn["password"],
                   database=dsn["database"], connect_timeout=3, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(DISTINCT {_safe_identifier(column)}) FROM {_safe_identifier(table)}")
            row = cur.fetchone()
            return int(row[0] if not isinstance(row, dict) else next(iter(row.values())) or 0)
    finally:
        conn.close()


def _occupied_count(dsn: dict, table: str, columns: list[str], connect) -> int:
    conn = connect(host=dsn["host"], port=dsn["port"], user=dsn["user"], password=dsn["password"],
                   database=dsn["database"], connect_timeout=3, autocommit=True)
    try:
        with conn.cursor() as cur:
            cols = ", ".join(_safe_identifier(column) for column in columns)
            cur.execute(f"SELECT COUNT(*) FROM {_safe_identifier(table)} WHERE " + " AND ".join(f"{_safe_identifier(c)} IS NOT NULL" for c in columns))
            row = cur.fetchone()
            return int(row[0] if not isinstance(row, dict) else next(iter(row.values())) or 0)
    finally:
        conn.close()


def build_dependency_plan(dsn: dict, target_table: str, target_rows: int, strategy: str = "direct_dependencies",
                          connect=pymysql.connect) -> dict:
    if strategy not in ("target_only", "direct_dependencies", "full_dependencies"):
        raise ValueError("不支持的依赖策略")
    cache = {}

    def metadata(table):
        if table not in cache:
            cache[table] = read_table_metadata(dsn, table, connect=connect)
        return cache[table]

    edges = []
    queue = deque([(target_table, 0)])
    seen = {target_table}
    while queue:
        child, depth = queue.popleft()
        for fk in metadata(child).get("foreign_keys") or []:
            parent = fk["REFERENCED_TABLE_NAME"]
            edge = {"from_table": parent, "from_column": fk["REFERENCED_COLUMN_NAME"],
                    "to_table": child, "to_column": fk["COLUMN_NAME"],
                    "constraint_name": fk.get("CONSTRAINT_NAME")}
            edges.append({**edge, "depth": depth + 1})
            if parent not in seen and strategy == "full_dependencies":
                seen.add(parent)
                queue.append((parent, depth + 1))
            elif parent not in seen and depth == 0 and strategy == "direct_dependencies":
                seen.add(parent)

    direct = [edge for edge in edges if edge["depth"] == 1]
    included = edges if strategy == "full_dependencies" else direct
    dependencies = []
    for edge in included:
        parent = edge["from_table"]
        if any(item["table"] == parent for item in dependencies):
            continue
        count = _table_count(dsn, parent, edge["from_column"], connect)
        dependencies.append({"table": parent, "depth": edge["depth"],
                             "reason": f"{edge['to_table']}.{edge['to_column']} -> {parent}.{edge['from_column']}",
                             "current_rows": count, "planned_rows": 0})

    unique_spaces = []
    target_meta = metadata(target_table)
    foreign_by_column = {fk["COLUMN_NAME"]: fk for fk in target_meta.get("foreign_keys") or []}
    for index in target_meta.get("unique_indexes") or []:
        columns = index.get("columns") or []
        sources = [foreign_by_column[column] for column in columns if column in foreign_by_column]
        if len(columns) < 2 or len(sources) != len(columns):
            continue
        counts = {column: _table_count(dsn, fk["REFERENCED_TABLE_NAME"], fk["REFERENCED_COLUMN_NAME"], connect)
                  for column, fk in zip(columns, sources)}
        total = math.prod(counts.values())
        occupied = _occupied_count(dsn, target_table, columns, connect)
        available = max(0, total - occupied)
        required = int(target_rows)
        space = {"table": target_table, "index": index["name"], "columns": columns,
                 "candidate_counts": counts, "total_space": total, "occupied_space": occupied,
                 "available_space": available, "required_rows": required,
                 "status": "sufficient" if available >= required else "insufficient"}
        unique_spaces.append(space)
        if strategy != "target_only" and available < required:
            needed = required + occupied
            names = list(counts)
            while math.prod(counts.values()) < needed:
                column = min(names, key=lambda name: counts[name])
                counts[column] += 1
            for dependency in dependencies:
                for edge in sources:
                    if edge["REFERENCED_TABLE_NAME"] == dependency["table"]:
                        dependency["planned_rows"] = max(dependency["planned_rows"], counts[names[sources.index(edge)]] - dependency["current_rows"])

    graph = defaultdict(set)
    indegree = defaultdict(int)
    nodes = {target_table} | {item["table"] for item in dependencies}
    for edge in included:
        if edge["from_table"] in nodes and edge["to_table"] in nodes and edge["to_table"] not in graph[edge["from_table"]]:
            graph[edge["from_table"]].add(edge["to_table"])
            indegree[edge["to_table"]] += 1
    ordered = deque(sorted(node for node in nodes if indegree[node] == 0))
    execution_order = []
    while ordered:
        node = ordered.popleft(); execution_order.append(node)
        for child in sorted(graph[node]):
            indegree[child] -= 1
            if indegree[child] == 0: ordered.append(child)
    warnings = []
    if len(execution_order) != len(nodes):
        warnings.append("检测到循环外键依赖，无法自动确定安全造数顺序")
        execution_order = []
    if strategy == "full_dependencies" and execution_order:
        minimum_rows = max([int(item.get("planned_rows") or 0) for item in dependencies] + [int(target_rows)])
        for dependency in dependencies:
            if dependency["planned_rows"] <= 0:
                dependency["planned_rows"] = minimum_rows
    for space in unique_spaces:
        if space["status"] == "insufficient":
            warnings.append(f"{space['table']}({', '.join(space['columns'])}) 当前剩余组合 {space['available_space']}，小于目标 {space['required_rows']}")
    return {"target": {"table": target_table, "rows": target_rows}, "dependencies": dependencies,
            "edges": edges, "unique_spaces": unique_spaces, "execution_order": execution_order,
            "strategy": strategy, "requires_confirmation": bool(dependencies), "warnings": warnings}


def build_parent_insert(metadata: dict) -> str:
    """Build conservative rules for a dependency table using only DB metadata."""
    values, columns = [], []
    foreign_keys = {fk["COLUMN_NAME"]: fk for fk in metadata.get("foreign_keys") or []}
    for column in metadata.get("columns") or []:
        name = column["name"]
        extra = str(column.get("extra") or "").lower()
        if "auto_increment" in extra or "generated" in extra or column.get("default") is not None:
            continue
        data_type = str(column.get("data_type") or "").lower()
        if name in foreign_keys:
            fk = foreign_keys[name]
            expr = f"sample('{fk['REFERENCED_TABLE_NAME']}','{fk['REFERENCED_COLUMN_NAME']}',{{'sample_ratio':100}})"
        elif column.get("enum_values"):
            choices = ",".join(repr(value) for value in column["enum_values"])
            expr = f"pick({choices})"
        elif data_type in ("tinyint", "smallint", "mediumint", "int", "integer", "bigint"):
            low, high = INTEGER_RANGES[data_type]
            if "unsigned" in str(column.get("column_type") or "").lower():
                low, high = 0, high * 2 + 1
            expr = f"rand({max(1, low)},{min(high, 1000000)})"
        elif data_type in ("decimal", "numeric", "float", "double"):
            expr = "randf(0,10000,2)"
        elif data_type in ("date",):
            expr = "randdate('2020-01-01','2030-12-31')"
        elif data_type in ("datetime", "timestamp"):
            expr = "randdt('2020-01-01 00:00:00','2030-12-31 23:59:59')"
        elif data_type in ("char", "varchar", "text", "tinytext", "mediumtext", "longtext"):
            length = min(int(column.get("max_length") or 32), 255)
            expr = f"randstr({max(1, length)})"
        elif column.get("nullable"):
            expr = "NULL"
        else:
            raise ValueError(f"依赖表 {metadata['table']} 字段 {name} 没有可用生成规则或默认值")
        columns.append(_safe_identifier(name))
        values.append(expr if expr == "NULL" else "{{" + expr + "}}")
    if not columns:
        raise ValueError(f"依赖表 {metadata['table']} 没有可自动生成的字段")
    return f"INSERT INTO {_safe_identifier(metadata['table'])} ({', '.join(columns)}) VALUES ({', '.join(values)});"
