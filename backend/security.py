from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


SESSION_COOKIE = "mc_seed_finder_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return "pbkdf2_sha256$260000$%s$%s" % (
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt_b64, digest_b64 = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _fernet_key() -> bytes:
    raw = os.getenv("APP_ENCRYPTION_KEY")
    if raw:
        try:
            key = base64.urlsafe_b64decode(raw)
            if len(key) == 32:
                return raw.encode()
        except Exception:
            pass
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    dev_secret = "dev-only-change-me-mc-seed-ai-finder"
    return base64.urlsafe_b64encode(hashlib.sha256(dev_secret.encode("utf-8")).digest())


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return Fernet(_fernet_key()).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return Fernet(_fernet_key()).decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def new_token() -> str:
    return secrets.token_urlsafe(32)
