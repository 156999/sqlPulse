import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import make_input, make_plan, plan_with_findings

from app.services.l1 import rules_plan
from app.services.l1.rules_plan import plan_healthy
from app.services.l1.thresholds import Thresholds

TH = Thresholds()


def ids(findings):
    return [f.rule_id for f in findings]


class TestDegradation:
    def test_c10_when_no_plan(self):
        out = rules_plan.rules(make_input(plan=None), TH)
        assert ids(out) == ["C10_plan_unavailable"]
        assert "产物缺失" in out[0].evidence[0].value or "产物" in out[0].evidence[0].value

    def test_c10_reason_when_disabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "explain_probe_enabled", False)
        out = rules_plan.rules(make_input(plan=None), TH)
        assert "EXPLAIN_PROBE_ENABLED" in out[0].evidence[0].value


class TestPlanRules:
    def test_healthy_plan_no_hit(self):
        assert rules_plan.rules(make_input(plan=make_plan()), TH) == []

    def test_c1_table_scan(self):
        plan = plan_with_findings("table_scan", table="orders")
        out = rules_plan.rules(make_input(plan=plan), TH)
        assert ids(out) == ["C1_plan_table_scan"]
        assert out[0].scope == {"sql_id": "sql_1", "table": "orders"}
        assert out[0].evidence[0].value == "sql_1"

    def test_c2_index_ignored(self):
        plan = plan_with_findings("index_ignored", table="orders")
        out = rules_plan.rules(make_input(plan=plan), TH)
        assert ids(out) == ["C2_plan_index_ignored"]
        assert "sql_1" in out[0].evidence[0].value

    def test_c4_c5_c6(self):
        for code, rid in (("filesort", "C4_plan_filesort"),
                          ("temporary", "C5_plan_temporary"),
                          ("join_buffer", "C6_plan_join_buffer")):
            plan = plan_with_findings(code, table="t2")
            assert ids(rules_plan.rules(make_input(plan=plan), TH)) == [rid]

    def test_c3_full_index_scan_is_info(self):
        plan = plan_with_findings("full_index_scan")
        out = rules_plan.rules(make_input(plan=plan), TH)
        assert out[0].severity == "info"

    def test_c7_large_scan(self):
        plan = make_plan()
        plan["summary"]["max_rows"] = 20000
        out = rules_plan.rules(make_input(plan=plan), TH)
        assert ids(out) == ["C7_plan_large_scan"]
        assert "20,000" in out[0].evidence[0].value

    def test_c8_worst_error(self):
        plan = make_plan()
        plan["summary"]["worst_level"] = "error"
        out = rules_plan.rules(make_input(plan=plan), TH)
        assert ids(out) == ["C8_plan_worst_error"]
        assert out[0].severity == "error"

    def test_c9_explain_error(self):
        plan = plan_with_findings("explain_error", table=None)
        out = rules_plan.rules(make_input(plan=plan), TH)
        assert ids(out) == ["C9_plan_explain_error"]


class TestPlanHealthy:
    def test_bookkeeping_does_not_break_health(self):
        plan = make_plan()
        plan["tasks"][0]["statements"][0]["findings"].append(
            {"code": "skipped_not_explainable", "level": "info", "table": None,
             "detail": "BEGIN 不可 EXPLAIN"})
        assert plan_healthy(plan) is True

    def test_quality_finding_breaks_health(self):
        assert plan_healthy(plan_with_findings("table_scan")) is False
        assert plan_healthy(None) is False
