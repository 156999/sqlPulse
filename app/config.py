from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    target_db_host: str = "localhost"
    target_db_port: int = 3306
    target_db_user: str = "root"
    target_db_password: str = ""
    target_db_name: str = "sqlpulse_demo"

    data_dir: Path = BASE_DIR / "data"
    logs_dir: Path = BASE_DIR / "logs"
    reports_dir: Path = BASE_DIR / "reports"

    datagen_script_execution_enabled: bool = False
    datagen_sql_timeout_sec: int = 300
    datagen_script_timeout_sec: int = 3600
    datagen_max_sql_chars: int = 5_000_000
    datagen_max_script_chars: int = 500_000

    # 压测结束后的 EXPLAIN 采集（只读、失败不影响报告，产物落 data/explain/）。
    # 名字里的 probe 是历史叫法 —— 2026-09-25 前它跑在"压测启动前"，见
    # docs/design/explain-collection.md「采集时机」。
    explain_probe_enabled: bool = True
    explain_max_statements: int = 50      # 单轮最多发多少条 EXPLAIN
    explain_budget_sec: float = 15.0      # 单轮采集总时间上限

    # ---- L1 硬规则报告阈值（见 sqlpulse/docs/l1_report_design.md §11）----
    # 平铺 + l1_ 前缀：.env 里直接写 L1_P99_MS=120 即可覆盖，单测也能用
    # Settings(l1_p99_ms=...) 注入。默认值待真实压测校准后固化。
    l1_p99_ms: float = 100.0
    l1_p99_severe_ms: float = 500.0
    l1_p95_ms: float = 50.0
    l1_tail_ratio: float = 10.0
    l1_sql_avg_ms: float = 50.0
    l1_sql_p99_ms: float = 500.0
    l1_err_rate: float = 0.01
    l1_err_rate_warn: float = 0.001
    l1_fail_share: float = 0.8
    l1_qps_factor: float = 0.5
    l1_plateau_slope: float = 0.05
    l1_cv: float = 0.5
    l1_duration_ratio: float = 0.8
    l1_min_requests: int = 1000
    l1_slow_total: float = 0.0
    l1_lock_total: float = 0.0
    l1_lock_rate: float = 10.0
    l1_tmp_total: float = 0.0
    l1_bufpool_hit: float = 0.95
    l1_bufpool_hit_bad: float = 0.90
    l1_conn_ratio: float = 0.8
    l1_tr_factor: float = 1.0
    l1_qps_amp: float = 3.0
    l1_min_points: int = 3
    l1_max_rows: int = 10000

    auth_enabled: bool = True
    app_secret_key: str = ""
    auth_session_ttl_seconds: int = 604800
    auth_session_cookie: str = "sqlpulse_session"
    auth_cookie_secure: bool = False
    auth_allow_register: bool = True
    auth_seed_root: bool = True

    mcp_user_id: Optional[str] = None
    mcp_username: Optional[str] = None
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765

    @model_validator(mode="after")
    def _require_secret_key_when_auth_enabled(self):
        if self.auth_enabled and not self.app_secret_key:
            raise ValueError("APP_SECRET_KEY must be set when AUTH_ENABLED=true")
        return self

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.logs_dir,
            self.data_dir / "locustfiles",
            self.data_dir / "locust",
            self.data_dir / "datagen",
            self.data_dir / "explain",
            self.reports_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
