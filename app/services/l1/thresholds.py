"""L1 阈值快照。冻结后随报告落库（reports.l1_json.thresholds），结论可追溯。"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Thresholds:
    p99_ms: float = 100.0
    p99_severe_ms: float = 500.0
    p95_ms: float = 50.0
    tail_ratio: float = 10.0
    sql_avg_ms: float = 50.0
    sql_p99_ms: float = 500.0
    err_rate: float = 0.01
    err_rate_warn: float = 0.001
    fail_share: float = 0.8
    qps_factor: float = 0.5
    plateau_slope: float = 0.05
    cv: float = 0.5
    duration_ratio: float = 0.8
    min_requests: int = 1000
    slow_total: float = 0.0
    lock_total: float = 0.0
    lock_rate: float = 10.0
    tmp_total: float = 0.0
    bufpool_hit: float = 0.95
    bufpool_hit_bad: float = 0.90
    conn_ratio: float = 0.8
    tr_factor: float = 1.0
    qps_amp: float = 3.0
    min_points: int = 3
    max_rows: int = 10000

    @classmethod
    def from_settings(cls, s) -> "Thresholds":
        return cls(
            p99_ms=s.l1_p99_ms, p99_severe_ms=s.l1_p99_severe_ms,
            p95_ms=s.l1_p95_ms, tail_ratio=s.l1_tail_ratio,
            sql_avg_ms=s.l1_sql_avg_ms, sql_p99_ms=s.l1_sql_p99_ms,
            err_rate=s.l1_err_rate, err_rate_warn=s.l1_err_rate_warn,
            fail_share=s.l1_fail_share, qps_factor=s.l1_qps_factor,
            plateau_slope=s.l1_plateau_slope, cv=s.l1_cv,
            duration_ratio=s.l1_duration_ratio, min_requests=s.l1_min_requests,
            slow_total=s.l1_slow_total, lock_total=s.l1_lock_total,
            lock_rate=s.l1_lock_rate, tmp_total=s.l1_tmp_total,
            bufpool_hit=s.l1_bufpool_hit, bufpool_hit_bad=s.l1_bufpool_hit_bad,
            conn_ratio=s.l1_conn_ratio, tr_factor=s.l1_tr_factor,
            qps_amp=s.l1_qps_amp, min_points=s.l1_min_points,
            max_rows=s.l1_max_rows,
        )

    def snapshot(self) -> dict:
        return asdict(self)
