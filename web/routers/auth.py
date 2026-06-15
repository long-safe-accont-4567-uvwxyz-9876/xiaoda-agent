from __future__ import annotations

import os
import time
import hashlib
import hmac
import base64
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from loguru import logger

from web.schemas import Envelope, LoginRequest, LoginResponse

router = APIRouter(tags=["auth"])

# Token store: token -> expiry
_tokens: dict[str, float] = {}
# Rate limit: ip -> (fail_count, lock_until)
_rate_limit: dict[str, tuple[int, float]] = {}

# Secret for HMAC
_SECRET_PATH = Path(__file__).parent.parent.parent / "credentials" / "webui_secret"
_SECRET: str = ""


def _load_or_create_secret() -> str:
    global _SECRET
    env_secret = os.getenv("WEBUI_SECRET", "")
    if env_secret:
        _SECRET = env_secret
        return _SECRET
    if _SECRET_PATH.exists():
        _SECRET = _SECRET_PATH.read_text().strip()
    else:
        _SECRET = secrets.token_hex(32)
        _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_PATH.write_text(_SECRET)
    return _SECRET


def _issue_token() -> tuple[str, float]:
    expiry = time.time() + 7 * 86400  # 7 days
    nonce = secrets.token_hex(8)
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()
    _tokens[token] = expiry
    return token, expiry


def _validate_token(token: str) -> bool:
    """Validate token via HMAC signature (survives restart)."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.rsplit(".", 2)
        if len(parts) != 3:
            return False
        expiry_str, nonce, sig = parts
        expiry = float(expiry_str)
        if expiry < time.time():
            return False
        payload = f"{expiry_str}.{nonce}"
        expected_sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        # Also register in memory for logout tracking
        _tokens[token] = expiry
        return True
    except Exception:
        return False


def _is_private_ip(ip: str) -> bool:
    """Check RFC1918 private IP or loopback."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    first = int(parts[0])
    second = int(parts[1])
    if first == 10:
        return True
    if first == 172 and 16 <= second <= 31:
        return True
    if first == 192 and second == 168:
        return True
    return False


async def get_current_user(request: Request) -> str:
    """Dependency: validate Bearer token. Returns user_id string."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = auth[7:]
    if not _validate_token(token):
        raise HTTPException(401, "Invalid or expired token")
    return "webui"


_load_or_create_secret()


@router.post("/auth/login", response_model=Envelope[LoginResponse])
async def login(req: LoginRequest, request: Request):
    password = os.getenv("WEBUI_PASSWORD", "")
    client_ip = request.client.host if request.client else "unknown"

    # Rate limit check
    if client_ip in _rate_limit:
        fails, lock_until = _rate_limit[client_ip]
        if time.time() < lock_until:
            raise HTTPException(429, "Too many login attempts, try again later")

    # No password set: only allow private IPs
    if not password:
        if not _is_private_ip(client_ip):
            raise HTTPException(403, "Public access denied without password")
        # Auto-login for LAN
        token, expiry = _issue_token()
        return Envelope(data=LoginResponse(token=token, expires_at=expiry))

    if req.password != password:
        # Rate limit
        fails, lock_until = _rate_limit.get(client_ip, (0, 0))
        fails += 1
        if fails >= 5:
            _rate_limit[client_ip] = (fails, time.time() + 600)
        else:
            _rate_limit[client_ip] = (fails, lock_until)
        raise HTTPException(401, "Invalid password")

    # Success: reset rate limit
    _rate_limit.pop(client_ip, None)
    token, expiry = _issue_token()
    return Envelope(data=LoginResponse(token=token, expires_at=expiry))


@router.post("/auth/logout", response_model=Envelope[None])
async def logout(user_id: str = Depends(get_current_user)):
    # In a full impl we'd invalidate the specific token
    return Envelope(data=None)
