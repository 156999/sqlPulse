from typing import Optional

import pymysql
from fastapi import HTTPException

from app import db
from app.models import DbDsn

ANONYMOUS_USER_ID = "anonymous"
_CONNECT_TIMEOUT = 3


def current_user_id(user: Optional[dict]) -> str:
    """AUTH_ENABLED=false 时统一归属匿名空间。"""
    return (user or {}).get("id") or ANONYMOUS_USER_ID


def to_public_connection(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "port": row["port"],
        "user": row["user"],
        "database": row["database"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "has_password": bool(row.get("password")),
    }


def resolve_connection(user_id: str, connection_id: str) -> DbDsn:
    row = db.get_connection(user_id, connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"连接 {connection_id} 不存在")
    return DbDsn(
        host=row["host"],
        port=row["port"],
        user=row["user"],
        password=row["password"],
        database=row["database"],
    )


def _without_password(message: str, password: str) -> str:
    if password:
        message = message.replace(password, "******")
    return message


def test_dsn(dsn: DbDsn) -> dict:
    """只执行 SELECT 1；返回结构固定为 {"ok": bool, "error": str?}。"""
    try:
        conn = pymysql.connect(
            host=dsn.host,
            port=dsn.port,
            user=dsn.user,
            password=dsn.password,
            database=dsn.database,
            connect_timeout=_CONNECT_TIMEOUT,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 统一转成用户可读结果
        return {"ok": False, "error": _without_password(str(e), dsn.password)}


def test_saved_connection(user_id: str, connection_id: str) -> dict:
    return test_dsn(resolve_connection(user_id, connection_id))
