"""Symmetric encryption helpers for at-rest secrets (SMTP passwords, Graph tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key from `Settings.secrets_encryption_key`.
In dev mode a fixed well-known key is used so local restarts do not require
configuration, but a clear warning is emitted at import time.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from config.settings import get_settings

logger = logging.getLogger(__name__)

# Fixed dev-only fallback key. Never used in production — production boots with
# `app_env=production` AND a non-empty `secrets_encryption_key`.
_DEV_FALLBACK_KEY = b"HWAFRMuxdKLC8Q8V8dUsAZnFabk0cazvC-G3E9jUHvU="
assert len(_DEV_FALLBACK_KEY) == 44, "Dev fallback key must be a 44-char Fernet key"


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    key = settings.secrets_encryption_key.strip()
    if not key:
        if str(settings.app_env).lower() == "production":
            raise RuntimeError(
                "SECRETS_ENCRYPTION_KEY is required when APP_ENV=production. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        logger.warning(
            "SECRETS_ENCRYPTION_KEY is empty — using a non-secret dev fallback key. "
            "DO NOT run this configuration in production."
        )
        return Fernet(_DEV_FALLBACK_KEY)
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # invalid key format
        raise RuntimeError(
            f"SECRETS_ENCRYPTION_KEY is not a valid Fernet key (44 url-safe-base64 chars): {exc}"
        ) from exc


def encrypt_str(plain: str) -> bytes:
    if plain is None:
        return b""
    return _fernet().encrypt(plain.encode("utf-8"))


def decrypt_str(blob: bytes | str | None) -> str:
    if not blob:
        return ""
    if isinstance(blob, str):
        try:
            blob_bytes = blob.encode("utf-8")
        except UnicodeEncodeError:
            return ""
    else:
        blob_bytes = blob
    try:
        return _fernet().decrypt(blob_bytes).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.exception("Failed to decrypt secret blob — returning empty string")
        return ""


def encrypt_dict(d: dict[str, Any]) -> bytes:
    if not d:
        return b""
    payload = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    return _fernet().encrypt(payload.encode("utf-8"))


def decrypt_dict(blob: bytes | str | None) -> dict[str, Any]:
    if not blob:
        return {}
    raw = decrypt_str(blob)
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        return {}


def merge_secrets(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Merge new secret keys into the existing dict (drop empty values from updates)."""
    merged = dict(existing)
    for key, value in updates.items():
        if value is None or value == "":
            continue
        merged[key] = value
    return merged
