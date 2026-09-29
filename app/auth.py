from typing import Optional
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from pwdlib import PasswordHash

from app import db
from app.config import settings


_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:
        return False


def get_current_user(request: Request) -> Optional[dict]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get_user(user_id)
    if user is None:
        request.session.pop("user_id", None)
    return user


def require_user(request: Request) -> Optional[dict]:
    if not settings.auth_enabled:
        return get_current_user(request)

    user = get_current_user(request)
    if user is not None:
        return user

    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    location = "/login?" + urlencode({"next": request.url.path})
    raise HTTPException(status_code=303, headers={"Location": location})
