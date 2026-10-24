import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.l1.contract import Finding, L1Input, L1Report, normalize_l1


class TestToJson:
    def test_serializable(self):
        rep = L1Report(
            verdict={"level": "healthy"}, findings=[Finding(
                "A1_p99_high", "ledger", "warn", "P99 超标",
                scope={"sql_id": "sql_1"},
                evidence=[("ledger", "P99", 120, "阈值 100")],
                root_cause="x", actions=[("sql", "y")],
            )],
            per_sql=[], engine_view={}, completeness={}, thresholds={"p99_ms": 100},
        )
        s = json.dumps(rep.to_json(), ensure_ascii=False)
        assert "A1_p99_high" in s

    def test_finding_roundtrip(self):
        f = Finding("X1", "cross", "warn", "t", scope={"sql_id": "sql_2"},
                    evidence=[("plan", "表", "orders", "")], root_cause="r",
                    actions=[("index", "建索引")])
        d = f.to_dict()
        assert d["rule_id"] == "X1"
        assert d["evidence"][0][1] == "表"


class TestNormalize:
    def test_new_shape_marks_not_legacy(self):
        raw = {"verdict": {"level": "healthy"}, "findings": [], "per_sql": []}
        out = normalize_l1(raw)
        assert out["legacy"] is False
        assert out["verdict"] == {"level": "healthy"}
        assert raw == {"verdict": {"level": "healthy"}, "findings": [], "per_sql": []}  # 输入不被改写

    def test_legacy_list(self):
        raw = [{"rule_id": "p99_high", "level": "warn", "title": "P99 高",
                "detail": "P99=120ms"}]
        out = normalize_l1(raw)
        assert out["legacy"] is True
        assert out["verdict"]["level"] == "legacy"
        assert out["findings"][0]["rule_id"] == "p99_high"
        assert out["findings"][0]["detail"] == "P99=120ms"
        assert out["findings"][0]["severity"] == "warn"

    def test_none_and_empty(self):
        assert normalize_l1(None)["legacy"] is True
        assert normalize_l1([])["findings"] == []

    def test_legacy_json_roundtrip(self):
        """旧报告落库的是 json.dumps(list)——模拟 get_report 的反序列化路径。"""
        raw = json.loads(json.dumps([{"rule_id": "err_rate", "level": "error",
                                      "title": "t", "detail": "d"}]))
        out = normalize_l1(raw)
        assert out["findings"][0]["severity"] == "error"


class TestL1InputDefaults:
    def test_defaults_are_none_safe(self):
        inp = L1Input(run={}, ledger={}, series={}, engine={}, plan=None,
                      target={}, tasks=[])
        assert inp.plan is None
        assert inp.series == {}
