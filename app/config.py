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

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.logs_dir, self.data_dir / "locustfiles", self.data_dir / "locust", self.logs_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
