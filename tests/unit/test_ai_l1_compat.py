"""L2 读取已保存的 L1 报告：兼容旧版 list 与新版 dict 两种形状。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.ai import tools
from app.config import settings

L0 = {
    "qps_avg": 12.5, "qps_peak": 20.0, "threads_running_p95": 3,
    "mysql_slow_total": 1, "mysql_lock_waits_total": 0,
}
LEGACY = [{"rule_id": "A1_low_qps", "level": "warn", "title": "旧规则", "detail": "legacy"}]
NEW = {
    "verdict": {"level": "warn", "title": "新规则"},
    "findings": [{"rule_id": "X7_root_not_executable", "severity": "error",
                  "title": "SQL 不可执行"}],
    "per_sql": [], "engine_view": {}, "completeness": {}, "thresholds": {},
}
BAD = {"findings": []}


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    old_conn = db._conn
    db._conn = None
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db.init_schema()
    yield
    db._conn = old_conn


def _prepare(run_id, l1):
    db.create_run({
        "id": run_id, "name": "l1-compat", "status": "finished",
        "sql_source": "paste", "sql_content": "SELECT 1;",
        "concurrency": 1, "spawn_rate": 1, "duration_sec": 1,
        "db_dsn_json": "{}", "created_at": "2026-11-08T00:00:00",
    })
    db.save_report(run_id, L0, l1, "/tmp/report.md")


def test_legacy_list_report_keeps_l1_and_l0(tmp_db):
    _prepare("aaaaaaaa", LEGACY)
    result = tools.get_run_metrics("aaaaaaaa")

    assert result["ok"] is True
    data = result["data"]
    assert [f["rule_id"] for f in data["l1_findings"]] == ["A1_low_qps"]
    assert data["summary"]["qps_avg"] == 12.5
    assert not any("损坏" in w for w in result["warnings"])


def test_new_dict_report_keeps_l1_and_l0(tmp_db):
    _prepare("bbbbbbbb", NEW)
    result = tools.get_run_metrics("bbbbbbbb")

    assert result["ok"] is True
    data = result["data"]
    assert [f["rule_id"] for f in data["l1_findings"]] == ["X7_root_not_executable"]
    assert data["summary"]["qps_avg"] == 12.5
    assert not any("损坏" in w for w in result["warnings"])


def test_unknown_l1_shape_falls_back_with_warning(tmp_db):
    _prepare("cccccccc", BAD)
    result = tools.get_run_metrics("cccccccc")

    assert result["ok"] is True
    data = result["data"]
    assert data["l1_findings"] is None
    assert data["summary"]["qps_avg"] is None
    assert any("结构不兼容" in w for w in result["warnings"])
