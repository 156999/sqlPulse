import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import healthy_comp

from app.services.l1.contract import Finding
from app.services.l1.rules_cross import verdict


def f(rule_id, severity):
    return Finding(rule_id, "ledger", severity, "t")


class TestLevels:
    def test_severe(self):
        v = verdict([f("A2", "error")], healthy_comp())
        assert v["level"] == "severe"

    def test_needs_optimization(self):
        v = verdict([f("A1", "warn")], healthy_comp())
        assert v["level"] == "needs_optimization"

    def test_attention(self):
        v = verdict([f("A13_low_sample", "info")], healthy_comp())
        assert v["level"] == "attention"

    def test_healthy_when_only_x8(self):
        v = verdict([f("X8_root_healthy", "info")], healthy_comp())
        assert v["level"] == "healthy"

    def test_not_usable_wins(self):
        v = verdict([f("X7_root_not_executable", "error"), f("A2", "error")],
                    healthy_comp())
        assert v["level"] == "not_usable"

    def test_empty(self):
        v = verdict([], healthy_comp())
        assert v["level"] == "healthy"


class TestConfidence:
    def test_high(self):
        v = verdict([], healthy_comp())
        assert v["confidence"] == "高"

    def test_medium_one_gap(self):
        v = verdict([], healthy_comp(sample_sufficient=False))
        assert v["confidence"] == "中"
        assert "样本不足" in v["summary"]

    def test_low_two_gaps(self):
        v = verdict([], healthy_comp(sample_sufficient=False, plan_available=False))
        assert v["confidence"] == "低"
        assert "样本不足" in v["summary"] and "无执行计划证据" in v["summary"]

    def test_healthy_with_low_confidence_hedges(self):
        v = verdict([], healthy_comp(metrics_sufficient=False))
        assert v["level"] == "healthy"
        assert "证据不足" in v["summary"]

    def test_all_gaps(self):
        v = verdict([], healthy_comp(sample_sufficient=False, plan_available=False,
                                     metrics_sufficient=False, duration_sufficient=False))
        assert v["confidence"] == "低"
