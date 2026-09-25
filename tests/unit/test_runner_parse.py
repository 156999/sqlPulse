import os
import re
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

    def _fill_fn(self, tmp_path, sql):
        """验证生成脚本可加载，并返回驱动格式化辅助函数。"""
        tasks = parse_sql_tasks(sql)
        out = self._render(tasks, tmp_path)
        env_keys = ["TARGET_DB_HOST", "TARGET_DB_USER", "TARGET_DB_PASSWORD", "TARGET_DB_NAME"]
        old = {k: os.environ.get(k) for k in env_keys}
        os.environ.update({k: "x" for k in env_keys})
        try:
            ns = {}
            exec(compile(out, "locustfile.py", "exec"), ns)
            from app.services.sql_params import compile_statement
            import pymysql
            connection = pymysql.connect(defer_connect=True)
            connection.server_status = 0
            cursor = connection.cursor()
            return lambda text: cursor.mogrify(*compile_statement(text).bind({}))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_valid_python(self, tmp_path):
        tasks = parse_sql_tasks(
            "-- weight: 70\nSELECT * FROM orders WHERE id = 1;\n-- weight: 30\nSELECT '含中文;分号' FROM t"
        )
        out = self._render(tasks, tmp_path)
        assert "@task(70)" in out and "@task(30)" in out
        assert 'self._exec("sql_1")' in out
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
        """验证驱动参数绑定后的字符串引号与数字。"""
        tasks = parse_sql_tasks("SELECT * FROM t WHERE id = {{rand(1,5)}};")
        out = self._render(tasks, tmp_path)
        env_keys = ["TARGET_DB_HOST", "TARGET_DB_USER", "TARGET_DB_PASSWORD", "TARGET_DB_NAME"]
        old = {k: os.environ.get(k) for k in env_keys}
        os.environ.update({k: "x" for k in env_keys})
        try:
            ns = {}
            exec(compile(out, "locustfile.py", "exec"), ns)
            fill = self._fill_fn(tmp_path, "SELECT 1")
            filled = fill("sku = {{pick('A','B')}} AND id = {{rand(1,5)}}")
            assert re.fullmatch(r"sku = '(A|B)' AND id = [1-5]", filled), filled
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_extended_placeholders_runtime_substitution(self, tmp_path):
        """randf / randstr / randdate / randdt / uuid 输出格式与引号规则。"""
        sql = (
            "SELECT {{randf(0.5,1.5,3)}}, {{randstr(10)}};"
            "\n-- weight: 5\nSELECT {{randdate('2026-01-01','2026-12-31')}};"
            "\n-- weight: 5\nSELECT {{randdt('2026-01-01 00:00:00','2026-01-02 00:00:00')}};"
            "\n-- weight: 5\nSELECT {{uuid()}};"
        )
        fill = self._fill_fn(tmp_path, sql)
        for _ in range(20):
            filled = fill(
                "SELECT {{randf(0.5,1.5,3)}}, {{randstr(10)}}, "
                "{{randdate('2026-01-01','2026-12-31')}}, "
                "{{randdt('2026-01-01 00:00:00','2026-01-02 00:00:00')}}, {{uuid()}}"
            )
            assert re.fullmatch(
                r"SELECT \d\.\d{3}, '[a-z0-9]{10}', "
                r"'[0-9]{4}-[0-9]{2}-[0-9]{2}', "
                r"'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}', "
                r"'[0-9a-f-]{36}'",
                filled,
            ), filled

    def test_pick_mixed_literals_quoting(self, tmp_path):
        """pick 支持数字/字符串混合，字符串自动补 SQL 引号。"""
        fill = self._fill_fn(tmp_path, "SELECT {{pick(1,'A',2.5)}};")
        for _ in range(30):
            picked = fill("{{pick(1,'A',2.5)}}")
            assert picked in ("1", "'A'", "2.5e0"), picked

    def test_pickw_weighted_choices(self, tmp_path):
        """pickw 只输出合法候选项（带引号），不出现权重或分隔符。"""
        fill = self._fill_fn(tmp_path, "SELECT {{pickw(('a',30),('b',70))}};")
        values = {fill("{{pickw(('a',30),('b',70))}}") for _ in range(60)}
        assert values <= {"'a'", "'b'"}
        assert values

    def test_invalid_placeholder_args_raise(self, tmp_path):
        """非法参数或倒置范围应抛 ValueError，由执行层记为请求失败。"""
        fill = self._fill_fn(tmp_path, "SELECT 1;")
        for bad in (
            "{{randf('a')}}",
            "{{randf(1,2,99)}}",
            "{{randdate('2026-05-01','2026-01-01')}}",
            "{{randdt('2026-05-01 00:00:00','2026-01-01 00:00:00')}}",
            "{{pickw(('a',-1),('b',1))}}",
        ):
            with pytest.raises(ValueError):
                fill(bad)
