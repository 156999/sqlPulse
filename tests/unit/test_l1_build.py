import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import (
    make_engine, make_input, make_ledger, make_plan, make_series,
    plan_with_findings,
)

from app import db
from app.config import settings
from app.services.l1 import build, evidence
from app.services.l1.thresholds import Thresholds

TH = Thresholds()


def ids(rep):
    return [f.rule_id for f in rep.findings]


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    old_conn = db._conn
    db._conn = None
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db.init_schema()
    yield
    db._conn = old_conn


class TestBuild:
    def test_healthy_baseline(self):
        rep = build(make_input(), TH)
        assert "X8_root_healthy" in ids(rep)
        assert rep.verdict["level"] == "healthy"
        assert rep.verdict["confidence"] == "高"
        assert rep.thresholds["p99_ms"] == 100.0

    def test_severity_ordering(self):
        """error 在 warn 前，warn 在 info 前；同级别 cross 优先。"""
        plan = plan_with_findings("table_scan", table="orders")
        ledger = make_ledger(
            p99_ms=600.0, total_requests=100, total_failures=0, err_rate=0.0)
        rep = build(make_input(ledger=ledger, plan=plan, series={}), TH)
        sevs = [f.severity for f in rep.findings]
        assert sevs == sorted(sevs, key=lambda s: {"error": 0, "warn": 1, "info": 2}[s])

    def test_per_sql_view_with_plan_and_hits(self):
        plan = plan_with_findings("table_scan", table="orders", sql_id="sql_1")
        ledger = make_ledger(per_sql=[{
            "sql_id": "sql_1", "sql": "SELECT * FROM orders WHERE a = 1",
            "requests": 5000, "failures": 0, "avg_ms": 120.0, "p50_ms": 60.0,
            "p95_ms": 200.0, "p99_ms": 480.0, "max_ms": 600.0, "qps": 80.0}])
        rep = build(make_input(ledger=ledger, plan=plan), TH)
        row = rep.per_sql[0]
        assert row["sql_id"] == "sql_1"
        assert "全表扫描" in row["plan_findings"]
        assert "X1_root_index_missing" in row["hit_rules"]

    def test_degraded_plan_still_builds(self):
        """EXPLAIN 缺失时报告照常生成，C10 + 低置信度。"""
        rep = build(make_input(plan=None), TH)
        assert "C10_plan_unavailable" in ids(rep)
        assert rep.completeness["plan_available"] is False
        assert rep.verdict["confidence"] == "中"

    def test_all_sources_missing_still_builds(self):
        rep = build(make_input(ledger={}, series={}, engine={}, plan=None,
                               target={}, run={}), TH)
        assert rep.verdict["confidence"] == "低"
        json.dumps(rep.to_json(), ensure_ascii=False)  # 可序列化


class TestEvidence:
    def test_collect_assembles_from_l0(self):
        l0 = make_ledger()
        run = {"id": "r", "name": "n", "status": "success", "sql_content": "SELECT 1",
               "groups_json": "[]", "concurrency": 10}
        inp = evidence.collect(run, l0, make_series(), make_plan(),
                               {"max_connections": 151})
        assert inp.engine["points"] == 60
        assert inp.engine["slow_present"] is True
        assert inp.target["max_connections"] == 151
        assert inp.tasks  # resolve_tasks 正常返回

    def test_completeness_gaps(self):
        inp = make_input(ledger=make_ledger(total_requests=50))
        comp = evidence.completeness(inp, TH)
        assert comp["sample_sufficient"] is False
        assert comp["metrics_sufficient"] is True

    def test_plan_reason_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "explain_probe_enabled", False)
        assert "EXPLAIN_PROBE_ENABLED" in evidence.plan_reason(None)


class TestDbCompatibility:
    def test_old_list_shape_roundtrip(self, tmp_db, tmp_path):
        """历史报告（list 形状 l1_json）落库后 get_report 归一化可读。"""
        legacy = [{"rule_id": "p99_high", "level": "warn", "title": "P99 高",
                   "detail": "P99=120ms"}]
        db.save_report("r-old", make_ledger(), legacy, str(tmp_path / "x.md"))
        got = db.get_report("r-old")
        assert got["l1"]["legacy"] is True
        assert got["l1"]["findings"][0]["severity"] == "warn"
        assert got["l1"]["findings"][0]["detail"] == "P99=120ms"

    def test_new_dict_shape_roundtrip(self, tmp_db, tmp_path):
        rep = build(make_input(), TH)
        db.save_report("r-new", make_ledger(), rep.to_json(),
                       str(tmp_path / "y.md"))
        got = db.get_report("r-new")
        assert got["l1"]["legacy"] is not True
        assert got["l1"]["verdict"]["level"] == "healthy"
        assert json.dumps(got["l1"], ensure_ascii=False)  # 读取侧可再序列化
