from pathlib import Path
from typing import Optional

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

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.logs_dir,
            self.data_dir / "locustfiles",
            self.data_dir / "locust",
            self.data_dir / "datagen",
            self.reports_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
