import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import (
    healthy_comp, make_engine, make_input, make_ledger, make_plan, make_series,
    plan_with_findings,
)

from app.services.l1 import rules_cross, rules_ledger, rules_mysql, rules_plan
from app.services.l1.thresholds import Thresholds

TH = Thresholds()


def ids(findings):
    return [f.rule_id for f in findings]


def _base(inp):
    out = []
    out += rules_ledger.rules(inp, TH)
    out += rules_mysql.rules(inp, TH)
    out += rules_plan.rules(inp, TH)
    return out


def slow_sql(sql_id="sql_2", avg=120.0, p99=480.0, failures=0):
    return {"sql_id": sql_id, "sql": "SELECT * FROM orders WHERE name LIKE '%x%'",
            "requests": 5000, "failures": failures, "avg_ms": avg,
            "p50_ms": 60.0, "p95_ms": 200.0, "p99_ms": p99, "max_ms": 600.0,
            "qps": 80.0}


class TestX1:
    def _inp(self):
        # sql_2 慢 + sql_2 的计划有 table_scan + index_ignored
        plan = plan_with_findings("table_scan", table="orders", sql_id="sql_2")
        plan["tasks"][0]["statements"][0]["findings"].append(
            {"code": "index_ignored", "level": "warn", "table": "orders",
             "detail": "possible_keys=customer_id key=NULL"})
        plan["summary"]["by_code"] = {"table_scan": 1, "index_ignored": 1}
        return make_input(
            ledger=make_ledger(p99_ms=480.0, per_sql=[
                {"sql_id": "sql_1", "sql": "SELECT 1", "requests": 5000,
                 "failures": 0, "avg_ms": 10.0, "p50_ms": 10.0, "p95_ms": 12.0,
                 "p99_ms": 15.0, "max_ms": 20.0, "qps": 50.0},
                slow_sql(),
            ]),
            engine=make_engine(lock_waits_total=320.0, lock_waits_rate_max=12.0),
            plan=plan,
        )

    def test_x1_hits_with_evidence_triad(self):
        inp = self._inp()
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X1_root_index_missing" in ids(cross)
        x1 = [f for f in cross if f.rule_id == "X1_root_index_missing"][0]
        sources = {e.source for e in x1.evidence}
        assert "ledger" in sources and "plan" in sources and "engine" in sources
        assert x1.scope == {"sql_id": "sql_2", "table": "orders"}

    def test_dedup_absorbs_children(self):
        """X1 命中后，构成它的 A5/A6/C1/C2 不再单独出现。"""
        inp = self._inp()
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        final = rules_cross.dedup(base, cross)
        final_ids = ids(final)
        assert "X1_root_index_missing" in final_ids
        for rid in ("A5_slow_statement", "A6_slow_statement_severe",
                    "C1_plan_table_scan", "C2_plan_index_ignored"):
            assert rid not in final_ids
        # 未参与 X1 的规则保留
        assert "A1_p99_high" in final_ids

    def test_no_x1_when_plan_clean(self):
        """慢语句但计划健康 → 不判 X1，A5/A6 保留。"""
        inp = make_input(ledger=make_ledger(p99_ms=480.0, per_sql=[slow_sql()]))
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X1_root_index_missing" not in ids(cross)
        final = rules_cross.dedup(base, cross)
        assert "A5_slow_statement" in ids(final)


class TestX2:
    def test_x2_lock_contention(self):
        inp = make_input(
            ledger=make_ledger(qps_peak=4.0, per_sql=[slow_sql(avg=30.0, p99=80.0)]),
            engine=make_engine(lock_waits_total=320.0, lock_waits_rate_max=12.0),
            plan=make_plan(),
        )
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X2_root_lock_contention" in ids(cross)
        final = rules_cross.dedup(base, cross)
        assert "B2_lock_waits" not in ids(final)
        assert "A9_throughput_below_expectation" not in ids(final)

    def test_no_x2_when_read_path_bad(self):
        """读路径有扫描问题时让位 X1（X2 不出）。"""
        inp = make_input(
            ledger=make_ledger(qps_peak=4.0, per_sql=[slow_sql()]),
            engine=make_engine(lock_waits_total=320.0),
            plan=plan_with_findings("table_scan", table="orders"),
        )
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X2_root_lock_contention" not in ids(cross)


class TestX3:
    def test_x3_tmp_disk(self):
        plan = plan_with_findings("filesort", table="orders")
        inp = make_input(
            ledger=make_ledger(per_sql=[slow_sql()]),
            engine=make_engine(tmp_disk_total=30.0),
            plan=plan,
        )
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X3_root_tmp_disk" in ids(cross)
        final = rules_cross.dedup(base, cross)
        assert "B4_tmp_disk_tables" not in ids(final)
        assert "C4_plan_filesort" not in ids(final)


class TestX5:
    def test_x5_write_path(self):
        """锁等待 + 读路径健康 + 账本无异常 → 写路径归因。"""
        inp = make_input(engine=make_engine(lock_waits_total=50.0))
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X5_root_write_path" in ids(cross)
        final = rules_cross.dedup(base, cross)
        assert "B2_lock_waits" not in ids(final)


class TestX6:
    def test_x6_conn_saturation(self):
        inp = make_input(
            ledger=make_ledger(qps_peak=4.0),
            engine=make_engine(threads_running_p95=130.0),
            target={"max_connections": 151},
        )
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X6_root_conn_saturation" in ids(cross)
        final = rules_cross.dedup(base, cross)
        assert "B7_conn_near_limit" not in ids(final)
        assert "A9_throughput_below_expectation" not in ids(final)


class TestX7:
    def test_x7_not_executable_by_err_rate(self):
        inp = make_input(ledger=make_ledger(err_rate=1.0, total_failures=10000))
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X7_root_not_executable" in ids(cross)
        x7 = [f for f in cross if f.rule_id == "X7_root_not_executable"][0]
        assert x7.severity == "error"

    def test_x7_not_executable_by_failed_status(self):
        inp = make_input(run={"id": "r", "name": "n", "status": "failed",
                              "concurrency": 10, "error_msg": "boom"})
        cross = rules_cross.apply_cross(inp, TH, [], healthy_comp())
        assert "X7_root_not_executable" in ids(cross)


class TestX8:
    def test_x8_healthy_requires_full_evidence(self):
        inp = make_input()
        base = _base(inp)
        cross = rules_cross.apply_cross(inp, TH, base, healthy_comp())
        assert "X8_root_healthy" in ids(cross)

    def test_x8_blocked_by_low_sample(self):
        inp = make_input(ledger=make_ledger(total_requests=100))
        base = _base(inp)
        cross = rules_cross.apply_cross(
            inp, TH, base, healthy_comp(sample_sufficient=False))
        assert "X8_root_healthy" not in ids(cross)

    def test_x8_blocked_by_missing_plan(self):
        inp = make_input(plan=None)
        base = _base(inp)
        cross = rules_cross.apply_cross(
            inp, TH, base, healthy_comp(plan_available=False))
        assert "X8_root_healthy" not in ids(cross)
