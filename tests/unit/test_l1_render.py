import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import (
    make_input, make_ledger, make_plan, plan_with_findings,
)

from app.services.l1 import build
from app.services.l1.render import build_view, render_markdown
from app.services.l1.thresholds import Thresholds

TH = Thresholds()
RUN = {"id": "r-test", "name": "订单查询压测", "status": "success",
       "concurrency": 100, "spawn_rate": 10, "duration_sec": 60,
       "started_at": "2026-09-30 10:00:00", "ended_at": "2026-09-30 10:01:00",
       "error_msg": None}


class TestMarkdownSections:
    def _md(self, **kw):
        inp = make_input(**kw)
        rep = build(inp, TH)
        return render_markdown(RUN, inp.ledger, rep.to_json()), rep

    def test_section_headers_present(self):
        md, _ = self._md()
        for h in ("# 压测诊断报告", "## 1. 结论摘要", "## 2. 问题清单",
                  "## 3. 分语句明细", "## 4. MySQL 侧观察",
                  "## 5. 采集完整性与局限", "## 6. 附录"):
            assert h in md, f"missing section: {h}"

    def test_findings_rendered_with_triad(self):
        plan = plan_with_findings("table_scan", table="orders")
        ledger = make_ledger(p99_ms=480.0, per_sql=[{
            "sql_id": "sql_1", "sql": "SELECT * FROM orders WHERE a = 1",
            "requests": 5000, "failures": 0, "avg_ms": 120.0, "p50_ms": 60.0,
            "p95_ms": 200.0, "p99_ms": 480.0, "max_ms": 600.0, "qps": 80.0}])
        md, rep = self._md(ledger=ledger, plan=plan)
        assert "X1_root_index_missing" in md or "根因" in md
        assert "[压测账本]" in md and "[执行计划]" in md
        assert "- 定位：" in md

    def test_empty_findings_note(self):
        md, rep = self._md()
        assert rep.findings  # X8 命中
        md_no_x8 = render_markdown(RUN, make_ledger(), {
            "verdict": {"level": "healthy", "confidence": "高", "title": "t", "summary": "s"},
            "findings": [], "per_sql": [], "engine_view": {},
            "completeness": {}, "thresholds": {},
        })
        assert "本轮无命中规则" in md_no_x8

    def test_verdict_header_line(self):
        md, _ = self._md()
        assert "L1 · 本地规则报告（未配置 LLM）" in md
        assert "置信度" in md

    def test_per_sql_table_has_plan_column(self):
        md, _ = self._md()
        assert "计划问题" in md and "命中规则" in md

    def test_limitations_listed(self):
        md, _ = self._md()
        assert "优化器估算" in md
        assert "long_query_time" in md


class TestLegacyMarkdown:
    def test_legacy_list_input(self):
        legacy = [{"rule_id": "p99_high", "level": "warn", "title": "P99 高",
                   "detail": "P99=120ms"}]
        md = render_markdown(RUN, make_ledger(), legacy)
        assert "旧版规则建议" in md
        assert "P99=120ms" in md

    def test_legacy_none(self):
        md = render_markdown(RUN, make_ledger(), None)
        assert "旧版" in md


class TestBuildView:
    def test_view_counts(self):
        rep = build(make_input(), TH).to_json()
        view = build_view(rep, make_ledger())
        assert view["available"] is True
        assert view["error_count"] == 0
        assert view["warn_count"] == 0
        assert view["verdict"]["level"] == "healthy"
        assert view["verdict_cls"] == "advice-ok"

    def test_view_finding_fields(self):
        plan = plan_with_findings("table_scan", table="orders")
        ledger = make_ledger(p99_ms=480.0, per_sql=[{
            "sql_id": "sql_1", "sql": "SELECT * FROM orders WHERE a = 1",
            "requests": 5000, "failures": 0, "avg_ms": 120.0, "p50_ms": 60.0,
            "p95_ms": 200.0, "p99_ms": 480.0, "max_ms": 600.0, "qps": 80.0}])
        rep = build(make_input(ledger=ledger, plan=plan), TH).to_json()
        view = build_view(rep, ledger)
        assert view["warn_count"] >= 1
        x1 = [f for f in view["findings"] if f["rule_id"] == "X1_root_index_missing"]
        assert x1, "X1 should be in view findings"
        assert x1[0]["scope_text"] == "sql_1 / orders"
        assert x1[0]["evidence"][0]["source_label"] in ("压测账本", "执行计划", "MySQL 引擎")
        assert all(a["layer_label"] for a in x1[0]["actions"])

    def test_view_legacy(self):
        legacy = [{"rule_id": "p99_high", "level": "warn", "title": "t", "detail": "d"}]
        view = build_view(legacy, make_ledger())
        assert view["legacy"] is True
        assert view["verdict"]["level"] == "legacy"
        assert view["per_sql"]  # fallback 到 l0.per_sql

    def test_view_completeness_card_data(self):
        rep = build(make_input(), TH).to_json()
        view = build_view(rep, make_ledger())
        assert view["completeness"]["plan_available"] is True
        assert view["completeness"]["metrics_points"] == 60
