from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, select_autoescape

from app.ui_language import LocaleTemplateLoader, ui_text

from app.auth import get_current_user
from app.config import settings


def auth_context(request):
    return {
        "current_user": get_current_user(request),
        "auth_enabled": settings.auth_enabled,
        "auth_allow_register": settings.auth_allow_register,
    }


env = Environment(
    loader=LocaleTemplateLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(),
)
env.filters["ui_text"] = ui_text
templates = Jinja2Templates(env=env, context_processors=[auth_context])
