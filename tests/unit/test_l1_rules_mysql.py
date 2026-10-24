import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.unit.l1_fixtures import make_engine, make_input, make_ledger

from app.services.l1 import rules_mysql
from app.services.l1.thresholds import Thresholds

TH = Thresholds()


def ids(findings):
    return [f.rule_id for f in findings]


class TestDegradation:
    def test_b10_exclusive_when_points_missing(self):
        """点数不足时 B10 独占输出，其余 B 层规则全部跳过。"""
        out = rules_mysql.rules(make_input(engine=make_engine(points=0)), TH)
        assert ids(out) == ["B10_metrics_missing"]

    def test_b10_threshold_boundary(self):
        assert ids(rules_mysql.rules(make_input(engine=make_engine(points=2)), TH)) == ["B10_metrics_missing"]
        assert ids(rules_mysql.rules(make_input(engine=make_engine(points=3)), TH)) == []


class TestCounters:
    def test_b1_slow_queries_present(self):
        out = rules_mysql.rules(make_input(engine=make_engine(slow_total=5.0)), TH)
        assert ids(out) == ["B1_slow_queries"]
        assert out[0].evidence[0].value == "5"

    def test_b1_zero_is_healthy_not_missing(self):
        """真实为 0（present=True）不触发；缺失（present=False）也不触发。"""
        assert rules_mysql.rules(make_input(engine=make_engine(slow_total=0.0)), TH) == []
        assert rules_mysql.rules(make_input(
            engine=make_engine(slow_total=None, slow_present=False)), TH) == []

    def test_b2_b3_lock_waits(self):
        out = rules_mysql.rules(make_input(engine=make_engine(
            lock_waits_total=5.0, lock_waits_rate_max=2.0)), TH)
        assert ids(out) == ["B2_lock_waits"]
        out = rules_mysql.rules(make_input(engine=make_engine(
            lock_waits_total=50.0, lock_waits_rate_max=11.0)), TH)
        assert ids(out) == ["B2_lock_waits", "B3_lock_waits_heavy"]
        assert out[1].severity == "error"

    def test_b4_tmp_disk(self):
        out = rules_mysql.rules(make_input(engine=make_engine(tmp_disk_total=3.0)), TH)
        assert ids(out) == ["B4_tmp_disk_tables"]

    def test_b5_b6_bufpool_two_tiers_only_one(self):
        out = rules_mysql.rules(make_input(engine=make_engine(bufpool_hit=0.93)), TH)
        assert ids(out) == ["B5_bufpool_hit_low"]
        assert out[0].severity == "info"
        out = rules_mysql.rules(make_input(engine=make_engine(bufpool_hit=0.85)), TH)
        assert ids(out) == ["B6_bufpool_hit_bad"]
        assert out[0].severity == "warn"


class TestConnections:
    def test_b7_conn_near_limit(self):
        # 并发给足，避免 B8（threads > concurrency）混进来干扰 B7 的断言
        inp = make_input(engine=make_engine(threads_running_p95=130.0),
                         run={"concurrency": 200},
                         target={"max_connections": 151})
        assert ids(rules_mysql.rules(inp, TH)) == ["B7_conn_near_limit"]
        # 0.79 → 不触发
        inp = make_input(engine=make_engine(threads_running_p95=120.0),
                         run={"concurrency": 200},
                         target={"max_connections": 151})
        assert rules_mysql.rules(inp, TH) == []

    def test_b7_no_max_conn_skips(self):
        inp = make_input(engine=make_engine(threads_running_p95=130.0),
                         run={"concurrency": 200}, target={})
        assert rules_mysql.rules(inp, TH) == []

    def test_b8_threads_exceed_concurrency(self):
        inp = make_input(engine=make_engine(threads_running_p95=15.0),
                         run={"concurrency": 10})
        assert ids(rules_mysql.rules(inp, TH)) == ["B8_threads_exceed_concurrency"]

    def test_b9_qps_amplification(self):
        inp = make_input(engine=make_engine(mysql_qps_avg=400.0),
                         ledger=make_ledger(qps_avg=100.0))
        assert ids(rules_mysql.rules(inp, TH)) == ["B9_mysql_qps_amplification"]
        inp = make_input(engine=make_engine(mysql_qps_avg=200.0),
                         ledger=make_ledger(qps_avg=100.0))
        assert rules_mysql.rules(inp, TH) == []
