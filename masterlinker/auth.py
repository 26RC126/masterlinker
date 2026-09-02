"""Web UI credentials.

Single-user by default: one account, created during setup. Flip
`web.multi_user` on and you can add as many as you like, each with a role.
Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt — never plain.

Sessions are stateless HMAC-signed tokens so restarting the bridge does not log
everyone out mid-net, and there is no session table to leak.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

PBKDF2_ROUNDS = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    _, actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected)


def make_user(username: str, password: str, role: str = "admin") -> dict[str, Any]:
    salt, digest = hash_password(password)
    return {"username": username, "salt": salt, "hash": digest, "role": role}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(secret: str, username: str, role: str, hours: int) -> str:
    payload = {"u": username, "r": role, "exp": int(time.time()) + hours * 3600}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def read_token(secret: str, token: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_unb64(sig), expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def authenticate(users: list[dict[str, Any]], username: str, password: str) -> dict[str, Any] | None:
    for user in users:
        if user["username"] == username and verify_password(password, user["salt"], user["hash"]):
            return user
    # Constant-ish work on failure so timing does not reveal valid usernames.
    hash_password(password, secrets.token_hex(16))
    return None
