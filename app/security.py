from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from .config import SECRET_PATH, TOKEN_TTL_SECONDS


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def load_or_create_secret(path: Path = SECRET_PATH) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secret = path.read_bytes()
        if len(secret) >= 32:
            return secret
    except FileNotFoundError:
        pass

    secret = secrets.token_bytes(48)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(secret)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    try:
        temporary.replace(path)
    except OSError:
        if path.exists():
            temporary.unlink(missing_ok=True)
            return path.read_bytes()
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=64)
    return f"scrypt${_SCRYPT_N}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64decode(salt_raw), n=int(n_raw), r=_SCRYPT_R, p=_SCRYPT_P, dklen=64
        )
        return hmac.compare_digest(digest, _b64decode(digest_raw))
    except (ValueError, TypeError, MemoryError):
        return False


def create_access_token(user_id: int, secret: bytes) -> str:
    payload = {"sub": user_id, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    encoded_payload = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def verify_access_token(token: str, secret: bytes) -> int | None:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected = hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_part)):
            return None
        payload = json.loads(_b64decode(payload_part))
        user_id = payload.get("sub")
        expires = payload.get("exp")
        if not isinstance(user_id, int) or not isinstance(expires, int) or expires <= int(time.time()):
            return None
        return user_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
