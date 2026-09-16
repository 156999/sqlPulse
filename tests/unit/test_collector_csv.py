import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.collector import read_last_history, read_per_sql_history

# locust --csv-full-history 输出的真实列结构（locust 2.46）
CSV_HEADER = (
    "Timestamp,User Count,Type,Name,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,"
    "99.9%,99.99%,100%,Total Request Count,Total Failure Count,Total Median Response Time,"
    "Total Average Response Time,Total Min Response Time,Total Max Response Time,"
    "Total Average Content Size\n"
)


def _write(tmp_path, rows):
    p = tmp_path / "x_stats_history.csv"
    p.write_text(CSV_HEADER + "".join(rows), encoding="utf-8")
    return p


class TestReadPerSqlHistory:
    def test_basic_and_aggregated_skipped(self, tmp_path):
        p = _write(tmp_path, [
            "100,5,SQL,sql_1,80.0,0.0,1,2,2,2,2,3,3,4,4,4,4,800,0,1,1.5,1,10,0\n",
            "100,5,SQL,sql_2,20.0,1.0,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,200,10,0,0.0,0,0,0\n",
            "100,5,,Aggregated,100.0,1.0,1,2,2,2,2,3,3,4,4,4,4,1000,10,1,1.2,1,10,0\n",
            "105,5,SQL,sql_1,90.0,0.0,1,2,2,2,2,3,3,5,5,5,5,900,0,1,1.5,1,12,0\n",
        ])
        out = read_per_sql_history(p)
        assert set(out) == {"sql_1", "sql_2"}  # Aggregated 被跳过
        assert len(out["sql_1"]) == 2
        assert out["sql_1"][0] == {"ts": 100, "rps": 80.0, "fails": 0.0, "avg_ms": 1.5, "p95_ms": 3.0, "p99_ms": 4.0}
        assert out["sql_1"][1]["p99_ms"] == 5.0
        # N/A 分位 -> None
        assert out["sql_2"][0]["p95_ms"] is None and out["sql_2"][0]["p99_ms"] is None
        assert out["sql_2"][0]["fails"] == 1.0

    def test_missing_file(self, tmp_path):
        assert read_per_sql_history(tmp_path / "nope.csv") == {}

    def test_non_sql_names_skipped(self, tmp_path):
        p = _write(tmp_path, [
            "100,5,GET,/index,10.0,0.0,1,1,1,1,1,1,1,1,1,1,1,10,0,1,1,1,1,0\n",
            "100,5,,SqlUser Total,10.0,0.0,1,1,1,1,1,1,1,1,1,1,1,10,0,1,1,1,1,0\n",
        ])
        assert read_per_sql_history(p) == {}


class TestReadLastHistoryCompat:
    def test_aggregated_last_row(self, tmp_path):
        p = _write(tmp_path, [
            "100,5,,Aggregated,50.0,0.0,1,2,2,2,2,3,3,4,4,4,4,500,5,1,2.0,1,9,0\n",
            "105,5,,Aggregated,60.0,0.0,1,2,2,2,2,4,4,5,5,5,5,600,6,1,2.5,1,9,0\n",
        ])
        h = read_last_history(p)
        assert h["qps"] == 60.0 and h["p99_ms"] == 5.0
        assert h["err_rate"] == 6 / 600
