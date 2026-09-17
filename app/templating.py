from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.config import settings


def auth_context(request):
    return {
        "current_user": get_current_user(request),
        "auth_enabled": settings.auth_enabled,
        "auth_allow_register": settings.auth_allow_register,
    }


templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates"),
    context_processors=[auth_context],
)
