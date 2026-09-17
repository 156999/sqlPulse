import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.sql_executor import execute_sql_job, split_sql_statements


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
        assert "第 1 条语句执行失败" in err

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
