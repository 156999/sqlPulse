"""用户自填模型 API Key 的加解密与校验。明文只在本模块的内存瞬间出现。

- 加密密钥由 APP_SECRET_KEY 派生（SHA-256 → urlsafe base64），不新增配置项；
  APP_SECRET_KEY 更换后旧密文将无法解密（decrypt 返回 None，页面提示重新填写）。
- 页面回显只用 mask() 的掩码；日志与接口永不输出明文。
"""
import base64
import hashlib
import re
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from app.config import settings

# compose / 本地开发默认值：用它派生密钥形同明文，保存时只告警不阻止。
_DEV_SECRETS = {"dev-only-secret-change-me", ""}
_MAX_KEY_LEN = 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def warn_default_secret() -> None:
    if settings.app_secret_key in _DEV_SECRETS:
        logger.warning(
            "APP_SECRET_KEY 为默认值或空值：用户 AI key 的落库加密形同明文，生产环境请先更换"
        )


def validate(api_key: str) -> Optional[str]:
    """返回错误文案；None 表示合法。"""
    if not api_key or not api_key.strip():
        return "API Key 不能为空"
    if len(api_key) > _MAX_KEY_LEN:
        return f"API Key 过长（最多 {_MAX_KEY_LEN} 字符）"
    if _CONTROL_RE.search(api_key):
        return "API Key 包含非法控制字符"
    return None


def mask(api_key: str) -> str:
    """回显用掩码：前 4 + 后 4；短 key 只显示 ****。"""
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}****{api_key[-4:]}"


def encrypt(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> Optional[str]:
    """解密失败（APP_SECRET_KEY 更换 / 数据损坏）返回 None，不抛异常。"""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
