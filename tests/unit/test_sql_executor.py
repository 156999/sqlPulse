import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.sql_executor import execute_sql_job, split_sql_statements
from app.services.sql_params import compile_statement, compile_variables


class TestSplitSqlStatements:
    def test_basic(self):
        stmts = split_sql_statements("INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);")
        assert stmts == ["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"]

    def test_semicolon_in_string(self):
        assert split_sql_statements("SELECT ';' AS a; SELECT `b;c` FROM t;") == [
            "SELECT ';' AS a",
            "SELECT `b;c` FROM t",
        ]

    def test_comments(self):
        sql = "-- line one\n# line two\nSELECT 1;\n/* block;\ncomment */ SELECT 2;\n-- tail"
        assert split_sql_statements(sql) == ["SELECT 1", "SELECT 2"]

    def test_delimiter_procedure(self):
        sql = (
            "DELIMITER $$\n"
            "CREATE PROCEDURE p()\n"
            "BEGIN\n"
            "  INSERT INTO t VALUES (1);\n"
            "  INSERT INTO t VALUES (2);\n"
            "END $$\n"
            "DELIMITER ;\n"
            "CALL p();"
        )
        stmts = split_sql_statements(sql)
        assert len(stmts) == 2
        assert stmts[0].startswith("CREATE PROCEDURE")
        assert "INSERT INTO t VALUES (1);" in stmts[0]
        assert "INSERT INTO t VALUES (2);" in stmts[0]
        assert stmts[1] == "CALL p()"

    def test_delimiter_in_string_not_handled(self):
        sql = "SELECT 'DELIMITER $$' AS x;"
        assert split_sql_statements(sql) == ["SELECT 'DELIMITER $$' AS x"]

    def test_seed_demo(self):
        text = (Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.sql").read_text(encoding="utf-8")
        stmts = split_sql_statements(text)
        assert len(stmts) == 13
        assert any(s.startswith("CREATE PROCEDURE seed_data") for s in stmts)
        assert not any(s.upper().startswith("DELIMITER") for s in stmts)

    def test_empty(self):
        assert split_sql_statements("-- only comment\n/* more */") == []


class TestExecuteSqlJob:
    def test_rows_accumulated(self, tmp_path):
        executed = []

        class FakeCursor:
            def __init__(self):
                self.rowcount = -1

            def execute(self, stmt):
                executed.append(stmt)
                self.rowcount = 7 if stmt.upper().startswith("INSERT") else -1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()
                self.closed = False

            def cursor(self):
                return self.cursor_obj

            def close(self):
                self.closed = True

        conn = FakeConn()

        def fake_connect(**kwargs):
            return conn

        status, rows, err = execute_sql_job(
            "job1",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            ["INSERT INTO t VALUES (1)", "SELECT 1"],
            tmp_path / "job1.log",
            threading.Event(),
            30,
            connect=fake_connect,
        )
        assert status == "finished"
        assert rows == 7
        assert err is None
        assert conn.closed is True

    def test_cancelled_stops_next(self, tmp_path):
        executed = []

        class FakeCursor:
            def __init__(self):
                self.rowcount = -1

            def execute(self, stmt):
                executed.append(stmt)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        event = threading.Event()
        event.set()
        status, rows, err = execute_sql_job(
            "job2",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            ["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"],
            tmp_path / "job2.log",
            event,
            30,
            connect=lambda **kwargs: FakeConn(),
        )
        assert status == "cancelled"
        assert executed == []

    def test_failure_reported(self, tmp_path):
        class FailingCursor:
            def execute(self, stmt):
                raise RuntimeError("boom")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self):
                return FailingCursor()

            def close(self):
                pass

        status, rows, err = execute_sql_job(
            "job3",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            ["INSERT INTO t VALUES (1)"],
            tmp_path / "job3.log",
            threading.Event(),
            30,
            connect=lambda **kwargs: FakeConn(),
        )
        assert status == "failed"
        assert "第 1 次执行失败（生成行 1，SQL 1）" in err

    def test_failure_log_includes_execution_location_and_masked_params(self, tmp_path):
        class FailingCursor:
            rowcount = -1

            def execute(self, stmt, args=None):
                raise RuntimeError(1062, "Duplicate entry 'dup' for key 'uk_code'")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self):
                return FailingCursor()

            def close(self):
                pass

        log_path = tmp_path / "job-diagnostic.log"
        statement = compile_statement(
            "INSERT INTO t(category_code,password_hash) VALUES ({{pick('dup')}},{{pick('secret')}})"
        )
        status, _, _ = execute_sql_job(
            "job-diagnostic",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [statement], log_path, threading.Event(), 30, connect=lambda **kwargs: FakeConn(), row_count=3,
        )
        log = log_path.read_text(encoding="utf-8")
        assert status == "failed"
        assert "execution 1/3 (generated-row 1/3, statement 1/1)" in log
        assert "mysql_error=1062" in log
        assert "failed params: ('dup', '******')" in log
        assert "secret" not in log

    def test_unique_guard_retries_database_conflict(self, tmp_path, monkeypatch):
        inserted = []

        class Cursor:
            rowcount = -1
            selected = None

            def execute(self, stmt, args=None):
                if stmt.startswith("SELECT 1 FROM `t`"):
                    self.selected = (1,) if args == (1,) else None
                    return
                inserted.append((stmt, args))
                self.rowcount = 1

            def fetchone(self):
                return self.selected

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        generated = iter([1, 2])
        monkeypatch.setattr("app.services.sql_params.random.randint", lambda *_: next(generated))
        log_path = tmp_path / "unique-retry.log"
        status, rows, error = execute_sql_job(
            "unique-job",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [compile_statement("INSERT INTO t(code) VALUES ({{rand(1,2)}})")],
            log_path, threading.Event(), 30, connect=lambda **kwargs: Connection(),
            target_table="t", unique_indexes=[{"name": "uk_code", "columns": ["code"]}],
        )
        assert status == "finished" and rows == 1 and error is None
        assert inserted == [("INSERT INTO t(code) VALUES (%s)", (2,))]
        assert "unique conflict index=uk_code candidate=(1,) source=database retry=1/20" in log_path.read_text(encoding="utf-8")

    def test_compound_unique_sample_conflict_uses_available_cached_combination(self, tmp_path, monkeypatch):
        inserted = []

        class Cursor:
            rowcount = -1
            selected = None

            def execute(self, stmt, args=None):
                if stmt.startswith("SELECT `id` FROM `orders`"):
                    self.selected = [(1,)]
                    return
                if stmt.startswith("SELECT `id` FROM `skus`"):
                    self.selected = [(2,), (3,)]
                    return
                if stmt.startswith("SELECT 1 FROM `items`"):
                    self.selected = (1,) if args == (1, 2) else None
                    return
                inserted.append((stmt, args))
                self.rowcount = 1

            def fetchone(self):
                return self.selected

            def fetchall(self):
                return self.selected

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        monkeypatch.setattr("app.services.sql_params.random.choice", lambda rows: rows[0])
        monkeypatch.setattr("app.services.sql_executor.random.shuffle", lambda values: None)
        log_path = tmp_path / "compound-unique-retry.log"
        status, rows, error = execute_sql_job(
            "compound-unique-job",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [compile_statement(
                "INSERT INTO items(order_id,sku_id) "
                "VALUES ({{sample('orders','id')}},{{sample('skus','id')}})"
            )],
            log_path, threading.Event(), 30, connect=lambda **kwargs: Connection(),
            target_table="items", unique_indexes=[{"name": "uk_order_sku", "columns": ["order_id", "sku_id"]}],
        )
        assert status == "finished" and rows == 1 and error is None
        assert inserted == [("INSERT INTO items(order_id,sku_id) VALUES (%s,%s)", (1, 3))]

    def test_compound_unique_sample_conflict_reports_exhausted_space(self, tmp_path, monkeypatch):
        inserted = []

        class Cursor:
            rowcount = -1
            selected = None

            def execute(self, stmt, args=None):
                if stmt.startswith("SELECT `id` FROM `orders`"):
                    self.selected = [(1,), (2,)]
                    return
                if stmt.startswith("SELECT `id` FROM `skus`"):
                    self.selected = [(1,), (2,), (3,)]
                    return
                if stmt.startswith("SELECT 1 FROM `items`"):
                    self.selected = (1,)
                    return
                inserted.append((stmt, args))
                self.rowcount = 1

            def fetchone(self):
                return self.selected

            def fetchall(self):
                return self.selected

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        monkeypatch.setattr("app.services.sql_params.random.choice", lambda rows: rows[0])
        monkeypatch.setattr("app.services.sql_executor.random.shuffle", lambda values: None)
        log_path = tmp_path / "compound-unique-exhausted.log"
        status, rows, error = execute_sql_job(
            "compound-unique-job",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [compile_statement(
                "INSERT INTO items(order_id,sku_id) "
                "VALUES ({{sample('orders','id')}},{{sample('skus','id')}})"
            )],
            log_path, threading.Event(), 30, connect=lambda **kwargs: Connection(),
            target_table="items", unique_indexes=[{"name": "uk_order_sku", "columns": ["order_id", "sku_id"]}],
        )
        assert status == "failed" and rows == 0
        assert "sample 候选组合已全部冲突" in error
        assert inserted == []
        assert "sample combination space exhausted probed=6" in log_path.read_text(encoding="utf-8")

    def test_empty_input_does_not_connect(self, tmp_path):
        def connect(**kwargs):
            raise AssertionError("should not connect")

        status, rows, err = execute_sql_job(
            "job4",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [],
            tmp_path / "job4.log",
            threading.Event(),
            30,
            connect=connect,
        )
        assert status == "failed"
        assert "没有可执行" in err

    def test_sample_placeholder_warms_cache_once_and_reuses_values(self, tmp_path, monkeypatch):
        executed = []
        sample_queries = []

        class FakeCursor:
            def __init__(self, conn):
                self.conn = conn
                self.rowcount = -1

            def execute(self, stmt, args=None):
                if stmt.startswith("SELECT `city` FROM `users`"):
                    sample_queries.append((stmt, args))
                    self.conn.last_rows = [("北京",), ("上海",)]
                    return
                executed.append((stmt, args))
                self.rowcount = 1

            def fetchall(self):
                return self.conn.last_rows

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def __init__(self):
                self.last_rows = []

            def cursor(self):
                return FakeCursor(self)

            def close(self):
                pass

        monkeypatch.setattr("app.services.sql_executor.random.choice", lambda rows: rows[0])
        status, rows, err = execute_sql_job(
            "job5",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [compile_statement("INSERT INTO t(city) VALUES ({{sample('users','city')}})")],
            tmp_path / "job5.log",
            threading.Event(),
            30,
            connect=lambda **kwargs: FakeConn(),
            row_count=2,
        )
        assert status == "finished"
        assert rows == 2
        assert err is None
        assert len(sample_queries) == 1
        assert executed == [
            ("INSERT INTO t(city) VALUES (%s)", ("北京",)),
            ("INSERT INTO t(city) VALUES (%s)", ("北京",)),
        ]

    def test_sample_variable_warms_cache_and_receives_sampler(self, tmp_path, monkeypatch):
        executed = []
        sample_queries = []

        class FakeCursor:
            rowcount = -1

            def execute(self, stmt, args=None):
                if stmt.startswith("SELECT `id` FROM `orders`"):
                    sample_queries.append((stmt, args))
                    return
                executed.append((stmt, args))
                self.rowcount = 1

            def fetchall(self):
                return [(42,)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr("app.services.sql_executor.random.choice", lambda rows: rows[0])
        definitions = compile_variables({"_f_order_id": "sample('orders','id')"})
        status, rows, err = execute_sql_job(
            "job-sample-variable",
            {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"},
            [compile_statement("INSERT INTO t(order_id) VALUES ({{var('_f_order_id')}})", definitions)],
            tmp_path / "job-sample-variable.log",
            threading.Event(),
            30,
            connect=lambda **kwargs: FakeConn(),
            variable_definitions=definitions,
        )
        assert status == "finished" and rows == 1 and err is None
        assert len(sample_queries) == 1
        assert executed == [("INSERT INTO t(order_id) VALUES (%s)", (42,))]
