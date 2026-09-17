from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import db
from app.auth import get_current_user, hash_password, verify_password
from app.config import settings
from app.templating import templates


router = APIRouter()

USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 128


def _safe_next(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("/") and not raw.startswith("//") and "\n" not in raw and "\r" not in raw:
        return raw
    return "/runs/new"


def validate_registration(username: str, password: str, password_confirm: str) -> Optional[str]:
    username = username.strip()
    if not USERNAME_MIN <= len(username) <= USERNAME_MAX:
        return f"用户名长度需为 {USERNAME_MIN} 到 {USERNAME_MAX} 个字符"
    if not username.isprintable():
        return "用户名包含非法字符"
    if password != password_confirm:
        return "两次输入的密码不一致"
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f"密码长度需为 {PASSWORD_MIN} 到 {PASSWORD_MAX} 个字符"
    return None


@router.get("/register")
def register_page(request: Request, next: str = ""):
    if get_current_user(request):
        return RedirectResponse("/runs/new", status_code=303)
    if not settings.auth_allow_register:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "register.html",
        {"error": None, "username": "", "next": _safe_next(next)},
    )


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if not settings.auth_allow_register:
        return RedirectResponse("/login", status_code=303)

    username = username.strip()
    error = validate_registration(username, password, password_confirm)
    if error:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": error, "username": username, "next": "/runs/new"},
            status_code=400,
        )
    if db.get_user_by_username(username):
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "用户名已存在", "username": username, "next": "/runs/new"},
            status_code=400,
        )

    user = db.create_user(username, hash_password(password))
    request.session["user_id"] = user["id"]
    return RedirectResponse("/runs/new", status_code=303)


@router.get("/login")
def login_page(request: Request, next: str = ""):
    if get_current_user(request):
        return RedirectResponse("/runs/new", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "username": "", "next": _safe_next(next)},
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    username = username.strip()
    user = db.get_user_by_username(username)
    if user is None or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误", "username": username, "next": _safe_next(next)},
            status_code=400,
        )

    request.session["user_id"] = user["id"]
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
