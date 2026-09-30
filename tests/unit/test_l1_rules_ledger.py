import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import make_input, make_ledger, make_series

from app.services.l1 import rules_ledger
from app.services.l1.thresholds import Thresholds

TH = Thresholds()


def ids(findings):
    return [f.rule_id for f in findings]


class TestAggregates:
    def test_healthy_baseline_no_hit(self):
        assert rules_ledger.rules(make_input(), TH) == []

    def test_a1_p99_boundary(self):
        assert rules_ledger.rules(make_input(ledger=make_ledger(p99_ms=100.0)), TH) == []
        # p50 抬高避免 A4（长尾比）混入，这里只测 A1 的边界
        out = rules_ledger.rules(make_input(ledger=make_ledger(p99_ms=100.1, p50_ms=20.0)), TH)
        assert ids(out) == ["A1_p99_high"]
        assert out[0].severity == "warn"

    def test_a2_p99_severe_error(self):
        out = rules_ledger.rules(make_input(ledger=make_ledger(p99_ms=501.0)), TH)
        assert "A2_p99_severe" in ids(out)
        f = [x for x in out if x.rule_id == "A2_p99_severe"][0]
        assert f.severity == "error"

    def test_a3_p95_high(self):
        out = rules_ledger.rules(make_input(ledger=make_ledger(p95_ms=51.0)), TH)
        assert ids(out) == ["A3_p95_high"]

    def test_a4_long_tail(self):
        # p99/p50 = 200/10 = 20 倍且 p99 > 100
        out = rules_ledger.rules(make_input(ledger=make_ledger(
            p99_ms=200.0, p50_ms=10.0)), TH)
        assert ids(out) == ["A1_p99_high", "A4_long_tail"]
        # 比值大但 p99 未过线 → 只有 A4 不触发
        assert rules_ledger.rules(make_input(ledger=make_ledger(
            p99_ms=90.0, p50_ms=1.0)), TH) == []

    def test_none_safe(self):
        ledger = make_ledger(p99_ms=None, p95_ms=None, p50_ms=None,
                             qps_peak=None, err_rate=None)
        assert rules_ledger.rules(make_input(ledger=ledger), TH) == []


class TestPerSql:
    def test_a5_slow_statement(self):
        per = [{"sql_id": "sql_2", "sql": "SELECT ...", "requests": 100,
                "failures": 0, "avg_ms": 51.0, "p50_ms": 40.0, "p95_ms": 60.0,
                "p99_ms": 70.0, "max_ms": 80.0, "qps": 10.0}]
        out = rules_ledger.rules(make_input(ledger=make_ledger(per_sql=per)), TH)
        assert ids(out) == ["A5_slow_statement"]
        assert out[0].scope == {"sql_id": "sql_2"}
        assert "sql_2" in out[0].title

    def test_a6_slow_statement_severe(self):
        per = [{"sql_id": "sql_3", "sql": "SELECT ...", "requests": 100,
                "failures": 0, "avg_ms": 30.0, "p50_ms": 25.0, "p95_ms": 60.0,
                "p99_ms": 600.0, "max_ms": 800.0, "qps": 10.0}]
        out = rules_ledger.rules(make_input(ledger=make_ledger(per_sql=per)), TH)
        assert ids(out) == ["A6_slow_statement_severe"]
        assert out[0].severity == "error"

    def test_a8_failures_concentrated(self):
        per = [
            {"sql_id": "sql_1", "sql": "SELECT 1", "requests": 500, "failures": 5,
             "avg_ms": 10.0, "p50_ms": 10.0, "p95_ms": 10.0, "p99_ms": 10.0,
             "max_ms": 10.0, "qps": 50.0},
            {"sql_id": "sql_2", "sql": "SELECT bad", "requests": 500, "failures": 95,
             "avg_ms": 10.0, "p50_ms": 10.0, "p95_ms": 10.0, "p99_ms": 10.0,
             "max_ms": 10.0, "qps": 50.0},
        ]
        out = rules_ledger.rules(make_input(ledger=make_ledger(per_sql=per)), TH)
        assert "A8_failures_concentrated" in ids(out)
        f = [x for x in out if x.rule_id == "A8_failures_concentrated"][0]
        assert f.scope == {"sql_id": "sql_2"}

    def test_a8_failures_spread_not_hit(self):
        per = [
            {"sql_id": "sql_1", "sql": "a", "requests": 500, "failures": 50,
             "avg_ms": 10.0, "p50_ms": 10.0, "p95_ms": 10.0, "p99_ms": 10.0,
             "max_ms": 10.0, "qps": 50.0},
            {"sql_id": "sql_2", "sql": "b", "requests": 500, "failures": 50,
             "avg_ms": 10.0, "p50_ms": 10.0, "p95_ms": 10.0, "p99_ms": 10.0,
             "max_ms": 10.0, "qps": 50.0},
        ]
        assert "A8_failures_concentrated" not in ids(
            rules_ledger.rules(make_input(ledger=make_ledger(per_sql=per)), TH))


class TestErrorsAndThroughput:
    def test_a7_err_rate_warn_then_error(self):
        out = rules_ledger.rules(make_input(ledger=make_ledger(err_rate=0.002)), TH)
        assert ids(out) == ["A7_err_rate"] and out[0].severity == "warn"
        out = rules_ledger.rules(make_input(ledger=make_ledger(err_rate=0.02)), TH)
        assert ids(out) == ["A7_err_rate"] and out[0].severity == "error"

    def test_a9_throughput_below_expectation(self):
        inp = make_input(ledger=make_ledger(qps_peak=4.0), run={"concurrency": 10})
        out = rules_ledger.rules(inp, TH)
        assert ids(out) == ["A9_throughput_below_expectation"]

    def test_a10_plateau(self):
        # 爬坡后走平 → 前半有爬坡证据、后半斜率 ≈ 0 → 平台期
        ramp_then_flat = {"sql_1": [
            {"ts": 1700000000 + i, "rps": float(min(10 * (i + 1), 50)), "fails": 0.0,
             "avg_ms": 12.0, "p95_ms": 20.0, "p99_ms": 30.0} for i in range(10)]}
        assert "A10_throughput_plateau" in ids(
            rules_ledger.rules(make_input(series=ramp_then_flat), TH))
        # 全程平稳（无爬坡证据）→ 不判平台期
        assert "A10_throughput_plateau" not in ids(
            rules_ledger.rules(make_input(series=make_series()), TH))
        # 持续增长 → 非平台
        growing = {"sql_1": [
            {"ts": 1700000000 + i, "rps": float(10 * (i + 1)), "fails": 0.0,
             "avg_ms": 12.0, "p95_ms": 20.0, "p99_ms": 30.0} for i in range(10)]}
        assert "A10_throughput_plateau" not in ids(
            rules_ledger.rules(make_input(series=growing), TH))

    def test_a10_too_few_points(self):
        assert "A10_throughput_plateau" not in ids(
            rules_ledger.rules(make_input(series=make_series(n=4)), TH))

    def test_a11_jitter(self):
        mixed = {"sql_1": [
            {"ts": 1700000000 + i, "rps": 100.0, "fails": 0.0, "avg_ms": 12.0,
             "p95_ms": 20.0, "p99_ms": 10.0 if i % 2 else 300.0}
            for i in range(10)]}
        assert "A11_stability_jitter" in ids(
            rules_ledger.rules(make_input(series=mixed), TH))
        assert "A11_stability_jitter" not in ids(
            rules_ledger.rules(make_input(series=make_series()), TH))


class TestMeta:
    def test_a12_duration_short(self):
        out = rules_ledger.rules(make_input(
            ledger=make_ledger(actual_duration_sec=30, duration_sec=60)), TH)
        assert ids(out) == ["A12_duration_short"]

    def test_a13_low_sample(self):
        out = rules_ledger.rules(make_input(
            ledger=make_ledger(total_requests=100)), TH)
        assert ids(out) == ["A13_low_sample"]
