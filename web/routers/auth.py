from __future__ import annotations

import os
import time
import json
import hashlib
import hmac
import base64
import secrets
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from loguru import logger

from web.schemas import Envelope, LoginRequest, LoginResponse

router = APIRouter(tags=["auth"])

# Token store: token -> expiry
_tokens: dict[str, float] = {}
# Rate limit: ip -> (fail_count, lock_until)
_rate_limit: dict[str, tuple[int, float]] = {}

# Secret for HMAC
_SECRET: str = ""

# 黑名单锁（文件读写线程安全）
_revoked_lock = Lock()


def _get_secret_path() -> Path:
    from config import get_credentials_dir
    return get_credentials_dir() / "webui_secret"


def _load_or_create_secret() -> str:
    global _SECRET
    env_secret = os.getenv("WEBUI_SECRET", "")
    if env_secret:
        _SECRET = env_secret
        return _SECRET
    secret_path = _get_secret_path()
    if secret_path.exists():
        _SECRET = secret_path.read_text(encoding="utf-8").strip()
    else:
        _SECRET = secrets.token_hex(32)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(_SECRET, encoding="utf-8")
    return _SECRET


def _get_revoked_path() -> Path:
    """黑名单文件路径。"""
    from config import get_credentials_dir
    return get_credentials_dir() / "revoked_tokens.json"


def _extract_expiry(token: str) -> float:
    """从 token 中提取过期时间。"""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.rsplit(".", 2)
        return float(parts[0]) if len(parts) == 3 else 0.0
    except Exception:
        return 0.0


def _revoke_token(token: str) -> None:
    """将 token 加入黑名单（持久化到文件）。"""
    with _revoked_lock:
        path = _get_revoked_path()
        data: dict[str, list] = {"revoked": []}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {"revoked": []}
                if not isinstance(data.get("revoked"), list):
                    data["revoked"] = []
            except Exception:
                data = {"revoked": []}
        if token not in data["revoked"]:
            data["revoked"].append(token)
        # 清理已过期的 revoked token（节省空间）
        now = time.time()
        data["revoked"] = [t for t in data["revoked"] if _extract_expiry(t) > now]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("auth.revoke_save_failed error={}", str(e))


def _is_revoked(token: str) -> bool:
    """检查 token 是否在黑名单中。"""
    path = _get_revoked_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return token in data.get("revoked", [])
    except Exception:
        return False


def _issue_token() -> tuple[str, float]:
    expiry = time.time() + 7 * 86400  # 7 days
    nonce = secrets.token_hex(8)
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()
    _tokens[token] = expiry
    return token, expiry


def _validate_token(token: str) -> bool:
    """Validate token via HMAC signature + revocation check."""
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
        # 检查黑名单
        if _is_revoked(token):
            return False
        # Also register in memory for tracking
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
    """Dependency: validate Bearer token. Returns user_id string.

    滑动续期：token 剩余不到1天时自动签发新 token，通过 request.state 传递，
    由中间件写入响应头 X-New-Token / X-New-Token-Expiry。
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = auth[7:]
    if not _validate_token(token):
        raise HTTPException(401, "Invalid or expired token")
    # 滑动续期：剩余不到1天时换新
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        expiry = float(decoded.rsplit(".", 2)[0])
        if expiry - time.time() < 86400:  # 不到1天就续
            new_token, new_expiry = _issue_token()
            _revoke_token(token)  # 旧 token 作废
            request.state.new_token = new_token
            request.state.new_expiry = new_expiry
            logger.info("auth.token_renewed old_expiry={} new_expiry={}", int(expiry), int(new_expiry))
    except Exception as e:
        logger.debug("auth.renew_check_failed error={}", str(e))
    return "webui"


_load_or_create_secret()


@router.post("/auth/login", response_model=Envelope[LoginResponse])
async def login(req: LoginRequest, request: Request) -> Any:
    password = os.getenv("WEBUI_PASSWORD", "")
    client_ip = request.client.host if request.client else "unknown"

    # Rate limit check
    if client_ip in _rate_limit:
        fails, lock_until = _rate_limit[client_ip]
        if time.time() < lock_until:
            remaining = int(lock_until - time.time())
            raise HTTPException(429, f"登录尝试过多，请 {remaining} 秒后重试")

    # No password set: only allow loopback (127.0.0.1) — block Docker bridge & LAN
    if not password:
        if client_ip != "127.0.0.1" and client_ip != "::1":
            raise HTTPException(403, "Public/LAN access denied without password. Set WEBUI_PASSWORD in .env")
        # Auto-login for localhost only
        token, expiry = _issue_token()
        return Envelope(data=LoginResponse(token=token, expires_at=expiry))

    if not hmac.compare_digest(req.password, password):
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
async def logout(user_id: str = Depends(get_current_user), request: Request = None) -> Any:
    """撤销当前 token（真正加入黑名单）。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        _revoke_token(token)
        _tokens.pop(token, None)
    return Envelope(data=None)


@router.post("/auth/revoke-all", response_model=Envelope[None])
async def revoke_all(user_id: str = Depends(get_current_user)) -> Any:
    """撤销所有 token（改密码后强制全量重新登录）。"""
    for token in list(_tokens.keys()):
        _revoke_token(token)
    _tokens.clear()
    return Envelope(data=None)
