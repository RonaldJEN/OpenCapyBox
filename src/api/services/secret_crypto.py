"""Small application secret envelope used by MCP credentials and approvals."""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from src.api.config import get_settings


_PREFIX = "v1:"
_logger = logging.getLogger(__name__)
_warned_debug_auth_fallback = False


def _fernet() -> Fernet:
    global _warned_debug_auth_fallback
    settings = get_settings()
    configured = str(getattr(settings, "mcp_secret_key", "") or "").strip()
    if configured:
        source = configured
    else:
        if not bool(getattr(settings, "debug", False)):
            raise RuntimeError(
                "MCP_SECRET_KEY must be configured when DEBUG=false; "
                "production credential encryption cannot fall back to AUTH_SECRET_KEY"
            )
        source = str(getattr(settings, "auth_secret_key", "") or "").strip()
        if not source:
            raise RuntimeError("AUTH_SECRET_KEY is unavailable for debug MCP key derivation")
        if not _warned_debug_auth_fallback:
            _logger.warning(
                "DEBUG 模式下 MCP_SECRET_KEY 未配置；"
                "正在从进程随机 AUTH_SECRET_KEY 派生 MCP 加密密钥。"
            )
            _warned_debug_auth_fallback = True
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    """Encrypt a UTF-8 value with a versioned application envelope."""

    if not isinstance(value, str):
        raise TypeError("secret value must be a string")
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    """Decrypt a value created by :func:`encrypt_secret`."""

    if not isinstance(value, str) or not value.startswith(_PREFIX):
        raise ValueError("unsupported encrypted secret format")
    try:
        return _fernet().decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ValueError("encrypted secret cannot be decrypted") from exc


def secret_fingerprint(value: str) -> str:
    """Return a non-reversible fingerprint suitable for change detection."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
