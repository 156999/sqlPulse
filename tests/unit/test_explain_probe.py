"""`explain_probe` 的单元测试。

全程**离线**：通过注入假 connector 替换 pymysql，所以不需要真实 MySQL。

假游标实现了与 pymysql 同名的 `mogrify(query, args)` —— 采集正是用它把
`%s` 参数渲染成字面量（`tests/unit/test_sql_params.py` 也依赖这个语义）。
"""
import json
import random
from pathlib import Path

import pytest

from app.services import explain_probe as ep
from app.services.runner import parse_sql_tasks

CONST_PLAN = [{
    "id": 1, "select_type": "SIMPLE", "table": "orders", "partitions": None,
    "type": "const", "possible_keys": "PRIMARY", "key": "PRIMARY", "key_len": "4",
    "ref": "const", "rows": 1, "filtered": 100.0, "Extra": None,
}]

FULL_SCAN_PLAN = [{
    "id": 1, "select_type": "SIMPLE", "table": "access_log", "partitions": None,
    "type": "ALL", "possible_keys": None, "key": None, "key_len": None,
    "ref": None, "rows": 51000, "filtered": 100.0, "Extra": "Using where",
}]

DSN = {"host": "127.0.0.1", "port": 3307, "user": "root",
       "password": "s3cret", "database": "sqlpulse_demo"}


def _literal(value):
    """够用的 pymysql 字面量渲染（只为断言，不追求完全等价）。"""
    if value is None:
        return "NULL"
    if type(value) is bool:
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.mogrified = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def mogrify(self, query, args=None):
        self.mogrified.append((query, args))
        if args is None:
            return query
        return query % tuple(_literal(a) for a in args)

    def execute(self, sql, args=None):
        self.conn.executed.append(sql)
        s = sql.strip()
        if s.upper().startswith("EXPLAIN "):
            self._rows = self.conn.plan_for(s)
        elif s.upper().startswith("SELECT VERSION()"):
            self._rows = [{"VERSION()": self.conn.version}]
        elif self.conn.is_sample_load(s):
            # SampleCache 的取样查询：`SELECT `col` FROM `tbl` ... LIMIT %s`
            if self.conn.sample_error is not None:
                raise self.conn.sample_error
            self._rows = self.conn.samples
        else:
            # 留痕而不是抛错：这样"只发过 EXPLAIN"的断言能直接查 executed / unexpected
            self.conn.unexpected.append(sql)
            self._rows = []
        return 1

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, plans=None, version="8.0.36", samples=None, sample_error=None):
        self.executed = []
        self.unexpected = []
        self.plans = plans or {}
        self.version = version
        self.samples = samples if samples is not None else [("SKU1", 1)]
        self.sample_error = sample_error
        self.closed = False

    @staticmethod
    def is_sample_load(sql):
        return sql.startswith("SELECT `")

    def plan_for(self, sql):
        for needle, val in self.plans.items():
            if needle in sql:
                if isinstance(val, BaseException):
                    raise val
                return val
        return CONST_PLAN

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def make_connector(plans=None, error=None, **conn_kw):
    conn = FakeConn(plans=plans, **conn_kw)

    def connect(dsn):
        if error is not None:
            raise error
        return conn

    return connect, conn


SAMPLE_SQL = """-- weight: 50
SELECT * FROM orders WHERE id = {{rand(1,10000)}};

-- weight: 30
SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%';

-- weight: 20
BEGIN;
UPDATE stock SET qty = qty - 1 WHERE sku = {{pick('SKU1','SKU2')}};
COMMIT;
"""


# ---------------------------------------------------------------- classify_plan

class TestClassifyPlan:
    def test_clean_index_lookup_has_no_findings(self):
        assert ep.classify_plan(CONST_PLAN) == []

    def test_full_scan_is_warned(self):
        f = ep.classify_plan(FULL_SCAN_PLAN)
        assert [x["code"] for x in f] == ["table_scan"]
        assert f[0]["level"] == "warn"
        assert "51000" in f[0]["detail"]

    def test_index_available_but_unused(self):
        rows = [{**FULL_SCAN_PLAN[0], "possible_keys": "idx_path"}]
        assert [x["code"] for x in ep.classify_plan(rows)] == ["table_scan", "index_ignored"]

    @pytest.mark.parametrize("acc", ["index", "INDEX"])
    def test_index_scan_of_whole_tree_is_only_info(self, acc):
        """回归：acc 被 .upper() 成大写后，比较时若写小写字面量 "index" 就永远匹配不上
        （2026-09-24 实测 `type=index` 被静默漏判，是单元测试抓出来的）。"""
        rows = [{**CONST_PLAN[0], "type": acc, "key": "idx_uid"}]
        f = ep.classify_plan(rows)
        assert [x["code"] for x in f] == ["full_index_scan"]
        assert f[0]["level"] == "info"

    def test_const_lookup_without_matching_row_is_clean(self):
        """真机实测：`WHERE id = <不存在的主键>` 时 MySQL 返回一整行 null +
        Extra='no matching row in const table'。这是最好的计划，不能误报。"""
        rows = [{"id": 1, "select_type": "SIMPLE", "table": None, "partitions": None,
                 "type": None, "possible_keys": None, "key": None, "key_len": None,
                 "ref": None, "rows": None, "filtered": None,
                 "Extra": "no matching row in const table"}]
        assert ep.classify_plan(rows) == []

    def test_full_scan_with_null_table_does_not_crash(self):
        """计划行可能没有 table 列（const 表被优化掉的场景），detail 里要能容错。"""
        rows = [{"table": None, "type": "ALL", "key": None,
                 "possible_keys": None, "rows": 4, "Extra": "Using where"}]
        f = ep.classify_plan(rows)
        assert [x["code"] for x in f] == ["table_scan"]
        assert "?" in f[0]["detail"]

    @pytest.mark.parametrize("extra,code", [
        ("Using filesort", "filesort"),
        ("Using temporary", "temporary"),
        ("Using where; Using join buffer (hash join)", "join_buffer"),
    ])
    def test_extra_red_lights(self, extra, code):
        rows = [{**CONST_PLAN[0], "Extra": extra}]
        assert code in [x["code"] for x in ep.classify_plan(rows)]

    def test_good_extra_tokens_are_not_flagged(self):
        for extra in ("Using index", "Using index condition", "Using where"):
            assert ep.classify_plan([{**CONST_PLAN[0], "Extra": extra}]) == []

    def test_case_insensitive_extra(self):
        rows = [{**CONST_PLAN[0], "Extra": "using filesort"}]
        assert [x["code"] for x in ep.classify_plan(rows)] == ["filesort"]

    def test_empty_plan(self):
        assert ep.classify_plan([]) == []
        assert ep.classify_plan(None) == []


# ------------------------------------------------------- 语句筛选（可 EXPLAIN 吗）

class TestStatementFilter:
    @pytest.mark.parametrize("sql", [
        "SELECT 1", "select 1", "SELECT 1;",
        "WITH c AS (SELECT 1) SELECT * FROM c",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t WHERE a = 1",
        "REPLACE INTO t VALUES (1)",
    ])
    def test_explainable(self, sql):
        assert ep.is_explainable(sql) is True

    @pytest.mark.parametrize("sql", [
        "BEGIN", "START TRANSACTION", "COMMIT", "ROLLBACK", "SAVEPOINT s1",
        "SET autocommit = 1", "USE db", "EXPLAIN SELECT 1", "", "   ",
    ])
    def test_not_explainable(self, sql):
        assert ep.is_explainable(sql) is False

    def test_leading_comment_is_skipped(self):
        assert ep.first_keyword("/*+ MAX_EXECUTION_TIME(1000) */ SELECT 1") == "SELECT"
        assert ep.first_keyword("-- hi\n# hi\nSELECT 1") == "SELECT"
        assert ep.first_keyword("/* a */ /* b */  UPDATE t SET a=1") == "UPDATE"


# ------------------------------------------------------------------- probe 主体

class TestProbeHappyPath:
    def _run(self, tmp_path, plans=None, **kw):
        tasks = parse_sql_tasks(SAMPLE_SQL)
        assert len(tasks) == 3, [t["sql_id"] for t in tasks]
        connect, conn = make_connector(plans=plans)
        res = ep.probe("run12345", tasks, DSN, connector=connect, out_dir=tmp_path, **kw)
        return res, conn

    def test_counts(self, tmp_path):
        """5 条语句：2 条 SELECT + 事务块里的 UPDATE 会被 EXPLAIN，BEGIN/COMMIT 跳过。"""
        res, _ = self._run(tmp_path)
        assert res["ok"] is True
        assert res["server_version"] == "8.0.36"
        s = res["summary"]
        assert s["tasks"] == 3
        assert s["statements"] == 5
        assert s["probed"] == 3
        assert s["skipped_not_explainable"] == 2
        assert s["dropped_by_limit"] == 0
        assert s["errors"] == 0

    def test_transaction_control_is_skipped_but_update_is_probed(self, tmp_path):
        res, _ = self._run(tmp_path)
        txn = res["tasks"][2]["statements"]
        assert txn[0]["findings"][0]["code"] == "skipped_not_explainable"   # BEGIN
        assert txn[1]["ok"] is True                                         # UPDATE
        assert txn[2]["findings"][0]["code"] == "skipped_not_explainable"   # COMMIT

    def test_only_read_only_statements_are_sent(self, tmp_path):
        """铁律 1：只发 EXPLAIN。这条断言是它的守门人。"""
        _, conn = self._run(tmp_path)
        assert conn.unexpected == [], f"发出了非预期语句：{conn.unexpected}"
        for sql in conn.executed:
            up = sql.strip().upper()
            assert up.startswith("EXPLAIN ") or up.startswith("SELECT VERSION()"), sql
            assert "ANALYZE" not in up, "绝不能对用户库执行 ANALYZE"
            assert "EXPLAIN ANALYZE" not in up, "EXPLAIN ANALYZE 会真的执行语句"

    def test_placeholder_is_bound_before_explain(self, tmp_path):
        """参数绑定是 feature-0916 的新语义：EXPLAIN 收到的是绑定后的完整 SQL。"""
        res, conn = self._run(tmp_path)
        stmt = res["tasks"][0]["statements"][0]
        assert "{{" in stmt["original"]
        assert "{{" not in stmt["filled"]
        assert "%s" not in stmt["filled"], "mogrify 必须把 %s 渲染成字面量"
        assert stmt["parameters"], "绑定的参数要记进产物"
        sent = [s for s in conn.executed if "orders" in s][0]
        assert "{{" not in sent and "%s" not in sent

    def test_literal_percent_is_not_mangled(self, tmp_path):
        """`LIKE '%api%'` 里的 % 会被编译器转义成 %%，mogrify 后必须还原。"""
        res, _ = self._run(tmp_path)
        filled = res["tasks"][1]["statements"][0]["filled"]
        assert filled == "SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%'"

    def test_same_run_is_reproducible(self, tmp_path):
        a, _ = self._run(tmp_path / "a")
        b, _ = self._run(tmp_path / "b")
        assert [s["filled"] for t in a["tasks"] for s in t["statements"]] == \
               [s["filled"] for t in b["tasks"] for s in t["statements"]]

    def test_different_run_id_may_differ_but_stays_valid(self, tmp_path):
        tasks = parse_sql_tasks("SELECT * FROM orders WHERE id = {{rand(1,10000)}};")
        connect, _ = make_connector()
        out = []
        for rid in ("aaa", "bbb"):
            res = ep.probe(rid, tasks, DSN, connector=connect, out_dir=tmp_path)
            out.append(res["tasks"][0]["statements"][0]["filled"])
        for sql in out:
            assert sql.startswith("SELECT * FROM orders WHERE id = ")
            assert sql.rsplit(" ", 1)[-1].isdigit()

    def test_worst_level_ignores_bookkeeping_findings(self, tmp_path):
        """跳过 BEGIN/COMMIT 会产生 info finding，但它不该把 worst_level 顶起来。"""
        res, _ = self._run(tmp_path)
        assert res["summary"]["worst_level"] is None
        codes = {f["code"] for t in res["tasks"] for s in t["statements"] for f in s["findings"]}
        assert codes == {"skipped_not_explainable"}

    def test_findings_aggregated(self, tmp_path):
        res, _ = self._run(tmp_path, plans={"access_log": FULL_SCAN_PLAN})
        s = res["summary"]
        assert s["by_code"]["table_scan"] == 1
        assert s["by_code"]["skipped_not_explainable"] == 2
        assert s["worst_level"] == "warn"
        assert s["max_rows"] == 51000
        assert s["max_rows_ref"] == {"sql_id": "sql_2", "index": 0}

    def test_artifact_roundtrip_and_no_password(self, tmp_path):
        res, _ = self._run(tmp_path)
        path = ep.artifact_path("run12345", tmp_path)
        assert path.is_file()
        assert ep.load_artifact("run12345", tmp_path) == res
        text = path.read_text(encoding="utf-8")
        assert "s3cret" not in text
        assert '"password"' not in text

    def test_plan_rows_are_normalised_to_contract_columns(self, tmp_path):
        res, _ = self._run(tmp_path)
        plan = res["tasks"][0]["statements"][0]["plan"][0]
        assert set(plan) == set(ep._PLAN_COLS)
        json.dumps(plan)   # 必须可 JSON 序列化

    def test_notes_are_carried(self, tmp_path):
        res, _ = self._run(tmp_path)
        assert res["notes"] == ep.NOTES
        assert res["probe_version"] == ep.PROBE_VERSION

    def test_variables_are_bound_into_statements(self, tmp_path):
        """变量定义（表单的 variables）与压测同源，采集也必须走同一套绑定。"""
        tasks = parse_sql_tasks("SELECT * FROM orders WHERE uid = {{var('uid')}};")
        connect, _ = make_connector()
        res = ep.probe("rv", tasks, DSN, variables={"uid": "rand(100,200)"},
                       connector=connect, out_dir=tmp_path)
        stmt = res["tasks"][0]["statements"][0]
        assert stmt["ok"] is True
        assert stmt["parameters"][0]["type"] == "int"
        assert 100 <= int(stmt["parameters"][0]["value"]) <= 200

    def test_sample_placeholder_reads_from_target_table(self, tmp_path):
        """`sample(...)` 需要从目标表取样本（只读），取样查询走 SampleCache。"""
        tasks = parse_sql_tasks("SELECT * FROM orders WHERE sku = {{sample('stock','sku')}};")
        connect, conn = make_connector(samples=[("SKU7", 1), ("SKU8", 1)])
        res = ep.probe("rs", tasks, DSN, connector=connect, out_dir=tmp_path)
        stmt = res["tasks"][0]["statements"][0]
        assert stmt["ok"] is True
        assert "'SKU7'" in stmt["filled"] or "'SKU8'" in stmt["filled"]
        assert any(c.startswith("SELECT `") for c in conn.executed), "取样查询应发出"
        assert conn.unexpected == []


class TestProbeErrors:
    def test_explain_failure_is_recorded_not_raised(self, tmp_path):
        tasks = parse_sql_tasks("SELECT * FROM nope; SELECT 1;")
        connect, _ = make_connector(
            plans={"nope": RuntimeError("1146: Table 'demo.nope' doesn't exist")})
        res = ep.probe("r1", tasks, DSN, connector=connect, out_dir=tmp_path)
        assert res["ok"] is True
        assert res["summary"]["errors"] == 1
        assert res["summary"]["probed"] == 1
        first = res["tasks"][0]["statements"][0]
        assert first["ok"] is False
        assert first["findings"][0]["code"] == "explain_error"
        assert "1146" in first["findings"][0]["detail"]
        assert first["filled"], "即使 EXPLAIN 失败也要留下实际发送的 SQL，便于排查"
        assert res["summary"]["worst_level"] == "warn"

    def test_bind_failure_does_not_abort_the_rest(self, tmp_path):
        """采样失败只毁掉那一条，后面的语句照常采集。"""
        tasks = parse_sql_tasks(
            "SELECT * FROM orders WHERE sku = {{sample('stock','sku')}};"
            "SELECT * FROM orders WHERE id = 1;")
        assert [t["sql_id"] for t in tasks] == ["sql_1", "sql_2"]
        connect, conn = make_connector(samples=[])   # 空样本 → bind 时抛错
        res = ep.probe("r1b", tasks, DSN, connector=connect, out_dir=tmp_path)
        assert res["ok"] is True
        assert res["error"] is None
        first = res["tasks"][0]["statements"][0]
        second = res["tasks"][1]["statements"][0]
        assert first["findings"][0]["code"] == "compile_error"
        assert first["ok"] is False
        assert second["ok"] is True                     # 后面这条照常采集
        assert res["summary"]["errors"] == 1
        assert res["summary"]["probed"] == 1
        assert [s for s in conn.executed if s.startswith("EXPLAIN")] == [
            "EXPLAIN SELECT * FROM orders WHERE id = 1"]   # 坏的那条从未发出去

    def test_compile_failure_is_recorded_not_raised(self, tmp_path):
        """占位符本身非法（rand 下限 > 上限）→ 编译阶段就失败，但采集仍不抛异常。"""
        tasks = parse_sql_tasks("SELECT * FROM orders WHERE id = {{rand(5,1)}};")
        connect, conn = make_connector()
        res = ep.probe("r1c", tasks, DSN, connector=connect, out_dir=tmp_path)
        assert res["ok"] is False
        assert "下限不能大于上限" in res["error"]
        assert res["tasks"] == []
        assert [s for s in conn.executed if s.startswith("EXPLAIN")] == []
        assert ep.load_artifact("r1c", tmp_path) == res, "编译失败也要落盘，页面才解释得清"

    def test_connect_failure_still_writes_artifact(self, tmp_path):
        tasks = parse_sql_tasks("SELECT 1;")
        connect, _ = make_connector(error=RuntimeError("Access denied for user 'root'"))
        res = ep.probe("r2", tasks, DSN, connector=connect, out_dir=tmp_path)
        assert res["ok"] is False
        assert "Access denied" in res["error"]
        assert res["summary"]["probed"] == 0
        assert res["tasks"] == []
        assert ep.load_artifact("r2", tmp_path) == res

    def test_missing_artifact_returns_none(self, tmp_path):
        assert ep.load_artifact("nope", tmp_path) is None

    def test_corrupted_artifact_returns_none(self, tmp_path):
        p = ep.artifact_path("bad", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not json", encoding="utf-8")
        assert ep.load_artifact("bad", tmp_path) is None

    def test_no_temporary_file_left_behind(self, tmp_path):
        tasks = parse_sql_tasks("SELECT 1;")
        connect, _ = make_connector()
        ep.probe("r3", tasks, DSN, connector=connect, out_dir=tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["r3.json"]

    def test_unsupported_value_type_is_still_json_safe(self, tmp_path):
        """产物必须永远可 JSON 序列化，否则整份采集白跑。"""
        tasks = parse_sql_tasks("SELECT * FROM orders WHERE b = {{pick('x')}};")
        connect, _ = make_connector()
        res = ep.probe("r7", tasks, DSN, connector=connect, out_dir=tmp_path,
                       variables={})
        assert ep.load_artifact("r7", tmp_path) == res


class TestProbeBudget:
    def test_max_statements_caps_and_marks_truncated(self, tmp_path):
        tasks = parse_sql_tasks("SELECT 1; SELECT 2; SELECT 3;")
        connect, conn = make_connector()
        res = ep.probe("r4", tasks, DSN, connector=connect, out_dir=tmp_path, max_statements=2)
        assert res["summary"]["probed"] == 2
        assert res["summary"]["dropped_by_limit"] == 1
        assert res["truncated"] is True
        assert res["truncated_reason"] == "max_statements=2"
        assert res["tasks"][2]["statements"][0]["findings"][0]["code"] == "not_probed"
        assert len([s for s in conn.executed if s.upper().startswith("EXPLAIN")]) == 2

    def test_no_truncation_when_limit_is_not_hit(self, tmp_path):
        tasks = parse_sql_tasks("SELECT 1; SELECT 2;")
        connect, _ = make_connector()
        res = ep.probe("r4b", tasks, DSN, connector=connect, out_dir=tmp_path, max_statements=5)
        assert res["truncated"] is False
        assert res["truncated_reason"] is None
        assert res["summary"]["dropped_by_limit"] == 0

    def test_zero_budget_probes_nothing(self, tmp_path):
        """预算在发 EXPLAIN 之前判，所以 budget=0 一条都不发。"""
        tasks = parse_sql_tasks("SELECT 1;")
        connect, conn = make_connector()
        res = ep.probe("r5", tasks, DSN, connector=connect, out_dir=tmp_path, budget_sec=0)
        assert res["summary"]["probed"] == 0
        assert res["summary"]["dropped_by_limit"] == 1
        assert res["truncated"] is True
        assert res["truncated_reason"] == "budget_sec=0"
        assert [s for s in conn.executed if s.upper().startswith("EXPLAIN")] == []

    def test_empty_task_list(self, tmp_path):
        connect, _ = make_connector()
        res = ep.probe("r6", [], DSN, connector=connect, out_dir=tmp_path)
        assert res["summary"]["statements"] == 0
        assert res["truncated"] is False
        assert res["ok"] is True


class TestSeeding:
    def test_seed_scope_restores_global_random_state(self):
        """`_seeded` 必须原样还原，否则会扰动同进程里并发的造数取值。"""
        random.seed(4321)
        before = random.getstate()
        with ep._seeded("whatever"):
            random.random()
        assert random.getstate() == before

    def test_seed_scope_restores_on_exception(self):
        random.seed(4321)
        before = random.getstate()
        with pytest.raises(RuntimeError):
            with ep._seeded("whatever"):
                raise RuntimeError("boom")
        assert random.getstate() == before

    def test_artifact_path_convention(self, tmp_path):
        assert ep.artifact_path("abc", tmp_path) == Path(tmp_path) / "abc.json"


class TestRunnerWiring:
    """runner 必须把 EXPLAIN 采集接在**压测结束后的收尾路径**上，
    且采集失败不能阻断报告生成。

    2026-09-25：采集时机从「`start()` 里、Popen 之前」改为「`_watch()` 里、
    `_on_finish` 之前」。这组用例随之翻转 —— 前两条现在是**回归测试**，
    防止有人再把调用点挪回 `start()`（那会重新阻塞启动）。
    """

    def _runner_src(self):
        return (Path(__file__).resolve().parents[2] / "app" / "services" / "runner.py"
                ).read_text(encoding="utf-8")

    def test_probe_is_not_in_start(self):
        """采集不许回到 `start()`：那会在 Popen 之前同步阻塞启动。"""
        src = self._runner_src()
        start = src[src.index("def start(self, run_id: str) -> None:"):
                    src.index("def stop(self, run_id: str) -> str:")]
        assert "explain_probe.probe(" not in start

    def test_probe_runs_after_status_persisted_and_before_report(self):
        """顺序：状态落库 → 采集 → 报告。报告侧要读产物，反了就会读到 None。"""
        src = self._runner_src()
        watch = src[src.index("def _watch(self"):src.index("def _all_failed(self")]
        assert watch.index('db.update_run(run_id, {"status": "finished"') < \
               watch.index("self._probe_explain_after_run(run_id)") < \
               watch.index("self._on_finish(run_id)")

    def test_probe_failure_cannot_break_report(self):
        """整段采集必须被 try/except 包住，否则会把收尾炸掉。"""
        src = self._runner_src()
        i = src.index("def _probe_explain_after_run")
        block = src[i:src.index("def _all_failed", i)]
        assert "except Exception" in block

    def test_probe_derives_inputs_from_run_row(self):
        """tasks / dsn 必须现推，不许让 `start()` 传——那会让收尾线程的契约变脆。"""
        src = self._runner_src()
        i = src.index("def _probe_explain_after_run")
        block = src[i:src.index("def _all_failed", i)]
        assert "resolve_tasks(run)" in block
        assert 'run["db_dsn_json"]' in block
        assert "PHASE_POST_RUN" in block

    def test_runner_gates_on_setting(self):
        """开关必须在采集方法里，而且要挡在真正发请求之前。"""
        src = self._runner_src()
        i = src.index("def _probe_explain_after_run")
        block = src[i:src.index("def _all_failed", i)]
        assert "if not settings.explain_probe_enabled:" in block
        assert block.index("if not settings.explain_probe_enabled:") < \
               block.index("explain_probe.probe(")

    def test_router_exposes_read_only_endpoint(self):
        src = (Path(__file__).resolve().parents[2] / "app" / "routers" / "explain.py"
               ).read_text(encoding="utf-8")
        assert '@router.get("/api/runs/{run_id}/explain")' in src
        assert "explain_probe.load_artifact" in src

    def test_probe_limits_come_from_settings(self):
        from app.config import settings
        assert settings.explain_max_statements == ep.DEFAULT_MAX_STATEMENTS
        assert settings.explain_budget_sec == ep.DEFAULT_BUDGET_SEC

    def test_resolve_tasks_is_shared_by_runner_and_probe(self):
        """采集与压测必须看同一份 tasks，否则产物里的 sql_id 对不上账本。"""
        from app.services import runner
        run = {"groups_json": None, "sql_content": "SELECT 1; SELECT 2;"}
        assert [t["sql_id"] for t in runner.resolve_tasks(run)] == ["sql_1", "sql_2"]
