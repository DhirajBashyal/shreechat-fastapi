"""Encryption for stored secrets (tenant AI provider keys).

Fernet (AES-128-CBC + HMAC). The key comes from AI_CREDENTIALS_KEY; in local
and test it falls back to a key derived from JWT_SECRET_KEY so development
works without extra setup. Outside local/test a missing key is an error.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

KEY_VERSION = 1


class SecretKeyMissingError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = settings.AI_CREDENTIALS_KEY
    if not key:
        if not settings.is_local or not settings.JWT_SECRET_KEY:
            raise SecretKeyMissingError("AI_CREDENTIALS_KEY is not configured")
        digest = hashlib.sha256(
            b"ai-credentials:" + settings.JWT_SECRET_KEY.encode()
        ).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes) -> str:
    try:
        return _fernet().decrypt(bytes(ciphertext)).decode()
    except InvalidToken as exc:
        raise SecretKeyMissingError(
            "Stored secret can't be decrypted with the current key"
        ) from exc
