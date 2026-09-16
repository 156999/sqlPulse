import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.rule_engine import evaluate


def _m(**kw):
    base = {
        "p99_ms": 50, "slowest_sql_avg_ms": 10, "slowest_sql_id": "sql_1",
        "threads_running_p95": 10, "max_connections": 151,
        "err_rate": 0.0, "qps_peak": 1000, "concurrency": 10,
    }
    base.update(kw)
    return base


class TestRules:
    def test_no_hit(self):
        assert evaluate(_m()) == []

    def test_p99_high(self):
        out = evaluate(_m(p99_ms=101))
        assert [a["rule_id"] for a in out] == ["p99_high"]
        assert out[0]["level"] == "warn"

    def test_slow_stmts(self):
        out = evaluate(_m(slowest_sql_avg_ms=51))
        assert [a["rule_id"] for a in out] == ["slow_stmts"]
        assert "sql_1" in out[0]["detail"]

    def test_conn_high(self):
        out = evaluate(_m(threads_running_p95=121, max_connections=151))
        assert [a["rule_id"] for a in out] == ["conn_high"]

    def test_conn_not_high(self):
        assert evaluate(_m(threads_running_p95=120, max_connections=151)) == []

    def test_err_rate(self):
        out = evaluate(_m(err_rate=0.02))
        assert [a["rule_id"] for a in out] == ["err_rate"]
        assert out[0]["level"] == "error"

    def test_qps_low(self):
        out = evaluate(_m(qps_peak=4, concurrency=10))
        assert [a["rule_id"] for a in out] == ["qps_low"]
        assert out[0]["level"] == "info"

    def test_multiple_hits(self):
        out = evaluate(_m(p99_ms=200, err_rate=0.5))
        assert {a["rule_id"] for a in out} == {"p99_high", "err_rate"}

    def test_none_safe(self):
        assert evaluate(_m(p99_ms=None, qps_peak=None)) == []

    def test_slow_sql_scenario(self):
        """slow.sql 场景：无索引 LIKE 全表扫描应命中 p99_high 或 slow_stmts"""
        out = evaluate(_m(p99_ms=150, slowest_sql_avg_ms=80))
        assert len(out) >= 2
