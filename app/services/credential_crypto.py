from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CredentialCryptoError(ValueError):
    pass


class CredentialKeyMissingError(CredentialCryptoError):
    pass


class CredentialDecryptError(CredentialCryptoError):
    pass


class CredentialCrypto:
    def __init__(self) -> None:
        self.current_version = (settings.credential_key_version or "v1").strip() or "v1"

    def _keys(self) -> dict[str, str]:
        key = settings.credential_key
        if not key:
            raise CredentialKeyMissingError("SQLPULSE_CREDENTIAL_KEY is not configured")
        return {self.current_version: key}

    def _cipher(self, version: str) -> Fernet:
        try:
            return Fernet(self._keys()[version].encode("ascii"))
        except KeyError as exc:
            raise CredentialDecryptError("credential key version is not configured") from exc
        except Exception as exc:
            raise CredentialKeyMissingError("SQLPULSE_CREDENTIAL_KEY is invalid") from exc

    def encrypt(self, value: str) -> str:
        plaintext = "" if value is None else str(value)
        token = self._cipher(self.current_version).encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{self.current_version}:{token}"

    def decrypt(self, value: str) -> str:
        if not self.is_encrypted(value):
            return "" if value is None else str(value)
        version, token = value.split(":", 1)
        try:
            return self._cipher(version).decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise CredentialDecryptError("credential ciphertext cannot be decrypted") from exc

    def is_encrypted(self, value: str) -> bool:
        if not isinstance(value, str):
            return False
        prefix, sep, token = value.partition(":")
        return bool(sep and prefix.startswith("v") and token.startswith("gAAAA"))


credential_crypto = CredentialCrypto()


def encrypt_dsn_snapshot(dsn: dict) -> dict:
    snapshot = dict(dsn)
    password = snapshot.pop("password", "")
    snapshot["password_ciphertext"] = credential_crypto.encrypt(password)
    return snapshot


def decrypt_dsn_snapshot(dsn: dict) -> dict:
    snapshot = dict(dsn)
    if "password_ciphertext" in snapshot:
        snapshot["password"] = credential_crypto.decrypt(snapshot.pop("password_ciphertext") or "")
    else:
        snapshot["password"] = snapshot.get("password", "")
    return snapshot
