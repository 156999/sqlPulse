"""旧 rule_engine.evaluate 兼容壳的行为锁定测试。

壳只返回 A/B 层非元规则（旧引擎没有 C/X 层与置信度声明），
规则 ID 换成了 L1 新 ID —— 这是刻意的行为变更（见
sqlpulse/docs/l1_report_implementation_guide.md §4.2）。
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.rule_engine import evaluate


def _m(**kw):
    base = {
        "p99_ms": 50, "per_sql": [], "threads_running_p95": 10,
        "max_connections": 151, "err_rate": 0.0, "qps_peak": 1000,
        "concurrency": 10,
    }
    base.update(kw)
    return base


def _run(metrics):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return evaluate(metrics)


def _ids(out):
    return [a["rule_id"] for a in out]


def _slow_per_sql(avg=51.0):
    return [{"sql_id": "sql_1", "sql": "SELECT * FROM t WHERE a = 1",
             "requests": 100, "failures": 0, "avg_ms": avg, "p50_ms": 40.0,
             "p95_ms": 60.0, "p99_ms": 70.0, "max_ms": 80.0, "qps": 10.0}]


class TestCompatShim:
    def test_no_hit(self):
        assert _run(_m()) == []

    def test_p99_high(self):
        out = _run(_m(p99_ms=101))
        assert "A1_p99_high" in _ids(out)
        assert out[0]["level"] == "warn"

    def test_slow_stmts(self):
        out = _run(_m(per_sql=_slow_per_sql()))
        assert "A5_slow_statement" in _ids(out)
        assert "sql_1" in out[0]["title"]

    def test_conn_high(self):
        out = _run(_m(threads_running_p95=121, max_connections=151))
        assert "B7_conn_near_limit" in _ids(out)

    def test_conn_not_high(self):
        out = _run(_m(threads_running_p95=120, max_connections=151))
        assert "B7_conn_near_limit" not in _ids(out)

    def test_err_rate(self):
        out = _run(_m(err_rate=0.02))
        assert "A7_err_rate" in _ids(out)
        assert out[0]["level"] == "error"

    def test_qps_low(self):
        out = _run(_m(qps_peak=4, concurrency=10))
        assert "A9_throughput_below_expectation" in _ids(out)
        assert out[0]["level"] == "info"

    def test_none_safe(self):
        assert _run(_m(p99_ms=None, qps_peak=None)) == []

    def test_multiple_hits(self):
        out = _run(_m(p99_ms=200, err_rate=0.5))
        assert {"A1_p99_high", "A7_err_rate"} <= set(_ids(out))

    def test_slow_sql_scenario(self):
        """slow.sql 场景：无索引 LIKE 全表扫描应同时命中延迟与慢语句规则"""
        out = _run(_m(p99_ms=150, per_sql=_slow_per_sql(avg=80.0)))
        assert {"A1_p99_high", "A5_slow_statement"} <= set(_ids(out))

    def test_excludes_cross_plan_and_meta_layers(self):
        out = _run(_m(p99_ms=101))
        assert all(a["rule_id"] != "X8_root_healthy" for a in out)
        assert all(a["rule_id"] != "C10_plan_unavailable" for a in out)
        assert all(a["rule_id"] != "A13_low_sample" for a in out)
