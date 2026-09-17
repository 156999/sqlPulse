from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DbDsn(BaseModel):
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "sqlpulse_demo"


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sql_source: str = "paste"
    sql_content: str = Field(min_length=1)
    concurrency: int = Field(ge=1, le=500)
    spawn_rate: int = Field(ge=1, le=500)
    duration_sec: int = Field(ge=5, le=3600)
    db_dsn: DbDsn


class RunOut(BaseModel):
    run_id: str
    name: str
    status: RunStatus
    sql_source: str
    concurrency: int
    spawn_rate: int
    duration_sec: int
    error_msg: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class MetricPoint(BaseModel):
    ts: int
    qps: Optional[float] = None
    avg_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    err_rate: Optional[float] = None
    threads_running: Optional[int] = None
    threads_connected: Optional[int] = None


class DataGenJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["sql", "shell"]
    source: Literal["paste", "file"] = "paste"
    content: str = Field(min_length=1)
    db_dsn: DbDsn
