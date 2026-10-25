"""sql_grouping 的单元测试：表名识别、分组保序、控制语句归组。"""

import pytest

from app.services.sql_grouping import (
    GLOBAL_GROUP,
    describe_statements,
    group_statements,
    statement_target_table,
)


class _Compiled:
    """模拟 compile_statement 的产出（带 .sql 属性）。"""

    def __init__(self, sql: str) -> None:
        self.sql = sql


@pytest.mark.parametrize("sql, expected", [
    ("INSERT INTO orders (a) VALUES (1);", "orders"),
    ("insert into orders (a) values (1)", "orders"),
    ("INSERT INTO `orders` (a) VALUES (1)", "orders"),
    ("INSERT IGNORE INTO orders (a) VALUES (1)", "orders"),
    ("INSERT INTO shop.orders (a) VALUES (1)", "orders"),
    ("INSERT INTO `shop`.`orders` (a) VALUES (1)", "orders"),
    ("INSERT HIGH_PRIORITY INTO access_log (a) VALUES (1)", "access_log"),
    ("REPLACE INTO stock (a) VALUES (1)", "stock"),
    ("UPDATE stock SET a = 1", "stock"),
    ("UPDATE LOW_PRIORITY stock SET a = 1", "stock"),
    ("UPDATE shop.stock SET a = 1", "stock"),
    ("DELETE FROM access_log WHERE id = 1", "access_log"),
    ("DELETE QUICK FROM access_log WHERE id = 1", "access_log"),
    ("CREATE TABLE IF NOT EXISTS `t2` (id INT)", "t2"),
    ("CREATE TEMPORARY TABLE tmp (id INT)", "tmp"),
    ("TRUNCATE TABLE orders", "orders"),
    ("TRUNCATE orders", "orders"),
    ("ALTER TABLE orders ADD COLUMN x INT", "orders"),
    ("DROP TABLE IF EXISTS tmp", "tmp"),
    ("/* 前导块注释 */ INSERT INTO orders (a) VALUES (1)", "orders"),
    ("-- 前导行注释\nINSERT INTO orders (a) VALUES (1)", "orders"),
    ("SET @x = 1", None),
    ("BEGIN", None),
    ("COMMIT", None),
    ("SHOW TABLES", None),
    ("SELECT 1", None),
])
def test_statement_target_table(sql, expected):
    assert statement_target_table(sql) == expected


def test_statement_target_table_accepts_compiled_object():
    assert statement_target_table(_Compiled("INSERT INTO orders (a) VALUES (1)")) == "orders"


def test_group_statements_buckets_control_statements_first():
    statements = [
        "SET @x = 1",
        "INSERT INTO orders (a) VALUES (1)",
        "INSERT INTO stock (b) VALUES (2)",
    ]

    groups = dict(group_statements(statements))

    assert list(groups) == [GLOBAL_GROUP, "orders", "stock"]
    assert groups[GLOBAL_GROUP] == ["SET @x = 1"]


def test_group_statements_keeps_statement_order_within_table():
    statements = [
        "INSERT INTO orders (a) VALUES (1)",
        "INSERT INTO stock (b) VALUES (2)",
        "INSERT INTO orders (a) VALUES (3)",
    ]

    groups = dict(group_statements(statements))

    assert groups["orders"] == [
        "INSERT INTO orders (a) VALUES (1)",
        "INSERT INTO orders (a) VALUES (3)",
    ]
    assert groups["stock"] == ["INSERT INTO stock (b) VALUES (2)"]


def test_group_statements_separates_tables_differing_only_by_case():
    groups = dict(group_statements(["INSERT INTO Orders (a) VALUES (1)",
                                    "INSERT INTO orders (a) VALUES (2)"]))

    assert sorted(groups) == ["Orders", "orders"]


def test_group_statements_accepts_compiled_objects():
    groups = dict(group_statements([
        _Compiled("INSERT INTO orders (a) VALUES (1)"),
        _Compiled("INSERT INTO stock (b) VALUES (2)"),
    ]))

    assert list(groups) == ["orders", "stock"]


def test_describe_statements_summarizes_groups():
    summary = describe_statements([
        "SET @x = 1",
        "INSERT INTO a (x) VALUES (1)",
        "INSERT INTO b (x) VALUES (2)",
        "INSERT INTO a (x) VALUES (3)",
    ])

    assert summary["total_statements"] == 4
    assert summary["groups"] == [{"table": "a", "statements": 2}, {"table": "b", "statements": 1}]
    assert summary["global_statements"] == 1


def test_describe_statements_without_recognisable_tables():
    summary = describe_statements(["SET @x = 1", "COMMIT"])

    assert summary["groups"] == []
    assert summary["global_statements"] == 2
    assert summary["total_statements"] == 2
