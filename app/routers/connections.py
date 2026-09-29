import sqlite3
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status

from app import db
from app.auth import require_user
from app.config import settings
from app.models import ConnectionCreate, ConnectionUpdate, DbDsn
from app.services import connection_manager
from app.templating import templates

router = APIRouter()


def _user_id(user: Optional[dict]) -> str:
    return connection_manager.current_user_id(user)


@router.get("/connections")
def connections_page(request: Request, user: Optional[dict] = Depends(require_user)):
    user_id = _user_id(user)
    default_dsn = {
        "host": settings.target_db_host,
        "port": settings.target_db_port,
        "user": settings.target_db_user,
        "password": "",
        "database": settings.target_db_name,
    }
    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "connections": [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)],
            "default_dsn": default_dsn,
        },
    )


@router.get("/api/connections")
def list_connections(user: Optional[dict] = Depends(require_user)):
    user_id = _user_id(user)
    return [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)]


@router.post("/api/connections", status_code=http_status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionCreate,
    user: Optional[dict] = Depends(require_user),
):
    user_id = _user_id(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="连接名称不能为空")
    now = datetime.now().isoformat(timespec="seconds")
    fields = {
        "id": uuid4().hex,
        "user_id": user_id,
        "name": name,
        "host": body.host,
        "port": body.port,
        "user": body.user,
        "password": body.password,
        "database": body.database,
        "created_at": now,
        "updated_at": now,
    }
    try:
        row = db.create_connection(user_id, fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f"连接名称“{name}”已存在")
    return connection_manager.to_public_connection(row)


@router.get("/api/connections/{connection_id}")
def get_connection(connection_id: str, user: Optional[dict] = Depends(require_user)):
    row = db.get_connection(_user_id(user), connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"连接 {connection_id} 不存在")
    return connection_manager.to_public_connection(row)


@router.put("/api/connections/{connection_id}")
def update_connection(
    connection_id: str,
    body: ConnectionUpdate,
    user: Optional[dict] = Depends(require_user),
):
    user_id = _user_id(user)
    existing = db.get_connection(user_id, connection_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"连接 {connection_id} 不存在")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="连接名称不能为空")
    fields = {
        "name": name,
        "host": body.host,
        "port": body.port,
        "user": body.user,
        "database": body.database,
    }
    if body.password:
        fields["password"] = body.password
    try:
        row = db.update_connection(user_id, connection_id, fields)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f"连接名称“{name}”已存在")
    if row is None:
        raise HTTPException(status_code=404, detail=f"连接 {connection_id} 不存在")
    return connection_manager.to_public_connection(row)


@router.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: str, user: Optional[dict] = Depends(require_user)):
    if not db.delete_connection(_user_id(user), connection_id):
        raise HTTPException(status_code=404, detail=f"连接 {connection_id} 不存在")
    return {"ok": True}


@router.post("/api/connections/{connection_id}/test")
def test_connection(connection_id: str, user: Optional[dict] = Depends(require_user)):
    return connection_manager.test_saved_connection(_user_id(user), connection_id)


@router.post("/api/connections/test")
def test_temporary_connection(dsn: DbDsn, user: Optional[dict] = Depends(require_user)):
    return connection_manager.test_dsn(dsn)
