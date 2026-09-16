import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.runner import parse_sql_tasks, render_locustfile, split_statements


class TestSplitStatements:
    def test_basic(self):
        assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]

    def test_semicolon_in_string(self):
        assert split_statements("SELECT ';' AS a; SELECT 2") == ["SELECT ';' AS a", "SELECT 2"]

    def test_comment_ignored(self):
        assert split_statements("-- hello\nSELECT 1;") == ["SELECT 1"]

    def test_trailing_no_semicolon(self):
        assert split_statements("SELECT 1") == ["SELECT 1"]

    def test_empty(self):
        assert split_statements("-- only comment\n\n") == []

    def test_backtick_identifier(self):
        assert split_statements("SELECT `a;b` FROM t; SELECT 2") == ["SELECT `a;b` FROM t", "SELECT 2"]


class TestParseSqlTasks:
    def test_weights(self):
        sql = (
            "-- weight: 70\nSELECT 1;\n\n-- weight: 20\nSELECT 2;\n\n-- weight: 10\nSELECT 3;"
        )
        tasks = parse_sql_tasks(sql)
        assert [t["weight"] for t in tasks] == [70, 20, 10]
        assert [t["sql_id"] for t in tasks] == ["sql_1", "sql_2", "sql_3"]
        assert tasks[0]["statements"] == ["SELECT 1"]

    def test_weight_with_trailing_comment(self):
        sql = "-- weight: 50 —— 走索引点查\nSELECT 1;\n-- weight: 10 - 慢查询\nSELECT 2;"
        tasks = parse_sql_tasks(sql)
        assert [t["weight"] for t in tasks] == [50, 10]

    def test_no_weight_defaults_to_1(self):
        tasks = parse_sql_tasks("SELECT 1;\nSELECT 2;")
        assert [t["weight"] for t in tasks] == [1, 1]

    def test_transaction_block(self):
        sql = "-- weight: 100\nBEGIN;\nUPDATE stock SET qty = qty - 1 WHERE sku = 'S1';\nCOMMIT;"
        tasks = parse_sql_tasks(sql)
        assert len(tasks) == 1
        assert len(tasks[0]["statements"]) == 3

    def test_transaction_without_weight(self):
        tasks = parse_sql_tasks("BEGIN; SELECT 1; COMMIT;")
        assert len(tasks) == 1
        assert len(tasks[0]["statements"]) == 3

    def test_empty_input(self):
        assert parse_sql_tasks("") == []
        assert parse_sql_tasks("-- weight: 5\n-- only comments") == []

    def test_leading_segment_before_first_weight(self):
        sql = "SELECT 0;\n-- weight: 9\nSELECT 1;"
        tasks = parse_sql_tasks(sql)
        assert len(tasks) == 2
        assert tasks[0]["weight"] == 1
        assert tasks[0]["statements"] == ["SELECT 0"]
        assert tasks[1]["weight"] == 9


class TestRenderLocustfile:
    def _render(self, tasks, tmp_path):
        """渲染到 pytest 临时目录，避免产物被 pytest 收集。"""
        return render_locustfile(tasks, tmp_path / "lf.py")

    def test_valid_python(self, tmp_path):
        tasks = parse_sql_tasks(
            "-- weight: 70\nSELECT * FROM orders WHERE id = 1;\n-- weight: 30\nSELECT '含中文;分号' FROM t"
        )
        out = self._render(tasks, tmp_path)
        assert "@task(70)" in out and "@task(30)" in out
        assert 'self._exec("sql_1"' in out
        compile(out, "locustfile.py", "exec")  # 语法合法

    def test_statements_escaping(self, tmp_path):
        tasks = [{"sql_id": "sql_1", "weight": 1, "statements": ["SELECT 'a\"b\\c'"]}]
        out = self._render(tasks, tmp_path)
        compile(out, "locustfile.py", "exec")

    def test_rand_placeholder_preserved(self, tmp_path):
        tasks = parse_sql_tasks(
            "SELECT * FROM orders WHERE id = {{rand(1,10000)}};"
            "\n-- weight: 5\nSELECT * FROM stock WHERE sku = {{pick('SKU1','SKU2')}};"
        )
        out = self._render(tasks, tmp_path)
        assert "{{rand(1,10000)}}" in out
        assert "pick(" in out  # tojson 将单引号转义为 \u0027，Python 运行时还原
        assert "@task(5)" in out
        compile(out, "locustfile.py", "exec")  # 语法合法

    def test_fill_runtime_substitution(self, tmp_path):
        """执行渲染产物，验证 _fill 占位符运行时替换正确（字符串保留引号）。"""
        tasks = parse_sql_tasks("SELECT * FROM t WHERE id = {{rand(1,5)}};")
        out = self._render(tasks, tmp_path)
        env_keys = ["TARGET_DB_HOST", "TARGET_DB_USER", "TARGET_DB_PASSWORD", "TARGET_DB_NAME"]
        old = {k: os.environ.get(k) for k in env_keys}
        os.environ.update({k: "x" for k in env_keys})
        try:
            ns = {}
            exec(compile(out, "locustfile.py", "exec"), ns)
            filled = ns["_fill"]("sku = {{pick('A','B')}} AND id = {{rand(1,5)}}")
            import re as _re
            assert _re.fullmatch(r"sku = '(A|B)' AND id = [1-5]", filled), filled
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
