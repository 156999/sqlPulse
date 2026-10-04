from typing import Optional

from app.services import connection_manager


def user_id_from_session(user: Optional[dict]) -> str:
    return connection_manager.current_user_id(user)
