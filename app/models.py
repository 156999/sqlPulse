from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


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


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=255)
    password: str = Field(default="", max_length=1024)
    database: str = Field(min_length=1, max_length=255)


class ConnectionUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    user: str = Field(min_length=1, max_length=255)
    password: str = Field(default="", max_length=1024)
    database: str = Field(min_length=1, max_length=255)


class ConnectionOut(BaseModel):
    id: str
    name: str
    host: str
    port: int
    user: str
    database: str
    created_at: str
    updated_at: str
    has_password: bool


class SqlGroup(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    execution_mode: Literal["autocommit", "transaction"] = "autocommit"
    weight: int = Field(default=1, gt=0, strict=True)
    sql: str = ""


class SqlInput(BaseModel):
    sql_content: Optional[str] = None
    groups: Optional[list[SqlGroup]] = None
    variables: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_source(self):
        if (self.sql_content is None) == (self.groups is None):
            raise ValueError("groups 与 sql_content 必须且只能提供一个")
        return self


class FormTools(BaseModel):
    action: Literal["import", "convert", "serialize", "normalize", "variables", "rename", "analyze"]
    sql_content: str = ""
    groups: list[SqlGroup] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)
    old_name: str = ""
    new_name: str = ""


class PreviewResult(BaseModel):
    ok: bool
    groups: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class TaskCreate(SqlInput):
    name: str = Field(min_length=1, max_length=100)
    sql_source: str = "paste"
    concurrency: int = Field(ge=1, le=500)
    spawn_rate: int = Field(ge=1, le=500)
    duration_sec: int = Field(ge=5, le=3600)
    connection_id: Optional[str] = None
    db_dsn: Optional[DbDsn] = None


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
    variables: dict[str, str] = Field(default_factory=dict)
    row_count: int = Field(default=1, ge=1, le=1_000_000)
    target_table: Optional[str] = Field(default=None, max_length=128)
    field_rules: list[dict] = Field(default_factory=list)
    connection_id: Optional[str] = None
    db_dsn: Optional[DbDsn] = None


class DataGenMetadataRequest(BaseModel):
    table: str = Field(min_length=1, max_length=128)
    connection_id: Optional[str] = None
    db_dsn: Optional[DbDsn] = None


class DataGenTableListRequest(BaseModel):
    connection_id: Optional[str] = None
    db_dsn: Optional[DbDsn] = None
    query: str = Field(default="", max_length=128)


class DataGenRuleValidationRequest(DataGenMetadataRequest):
    rules: list[dict] = Field(default_factory=list)
    row_count: int = Field(default=1, ge=1, le=1_000_000)
