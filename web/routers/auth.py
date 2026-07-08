from __future__ import annotations

import os
import time
import json
import hashlib
import hmac
import base64
import secrets
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Depends
from loguru import logger

from web.schemas import Envelope, LoginRequest, LoginResponse

router = APIRouter(tags=["auth"])

_tokens: "OrderedDict[str, float]" = OrderedDict()
_TOKENS_MAX_SIZE = 1000
_rate_limit: "OrderedDict[str, tuple[int, float]]" = OrderedDict()
_RATE_LIMIT_MAX_SIZE = 1000

_SECRET: str = ""

_secret_lock = Lock()
_revoked_lock = Lock()
_tokens_lock = Lock()
_rate_limit_lock = Lock()
# 已撤销 token 内存缓存，避免每次请求都读文件
_revoked_cache: set[str] = set()
_revoked_cache_mtime: float = 0.0


def _get_secret_path() -> Path:
    from config import get_credentials_dir
    return get_credentials_dir() / "webui_secret"


def _load_or_create_secret() -> str:
    global _SECRET
    with _secret_lock:
        if _SECRET:
            return _SECRET
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
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass
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
    except Exception as exc:
        logger.debug("auth.extract_expiry_failed: {}", exc, exc_info=True)
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
            except Exception as exc:
                logger.debug("auth.revoke_json_parse_failed: {}", exc, exc_info=True)
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
    global _revoked_cache, _revoked_cache_mtime
    path = _get_revoked_path()
    if not path.exists():
        return False
    try:
        mtime = path.stat().st_mtime
        with _revoked_lock:
            if mtime != _revoked_cache_mtime:
                data = json.loads(path.read_text(encoding="utf-8"))
                _revoked_cache = set(data.get("revoked", []))
                _revoked_cache_mtime = mtime
            return token in _revoked_cache
    except Exception as exc:
        logger.debug("auth.is_revoked_json_parse_failed: {}", exc, exc_info=True)
        return False


def _cleanup_expired_tokens() -> None:
    """清理已过期的 token，防止 _tokens 无限增长。"""
    now = time.time()
    expired = [t for t, exp in _tokens.items() if exp < now]
    for t in expired:
        _tokens.pop(t, None)


def _issue_token() -> tuple[str, float]:
    expiry = time.time() + 7 * 86400  # 7 days
    nonce = secrets.token_hex(8)
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()
    with _tokens_lock:
        _cleanup_expired_tokens()
        _tokens[token] = expiry
        _tokens.move_to_end(token)
        while len(_tokens) > _TOKENS_MAX_SIZE:
            _tokens.popitem(last=False)
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
        with _tokens_lock:
            _tokens[token] = expiry
            _tokens.move_to_end(token)
        return True
    except Exception as exc:
        logger.debug("auth.validate_token_failed: {}", exc, exc_info=True)
        return False


def _is_private_ip(ip: str) -> bool:
    """Check RFC1918 private IP or loopback."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        first = int(parts[0])
        second = int(parts[1])
    except (ValueError, IndexError):
        return False
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


def _cleanup_expired_rate_limits() -> None:
    """清理已过期的 rate limit 条目，防止 _rate_limit 无限增长。"""
    now = time.time()
    expired = [ip for ip, (_, lock_until) in _rate_limit.items() if lock_until < now]
    for ip in expired:
        _rate_limit.pop(ip, None)


@router.post("/auth/login", response_model=Envelope[LoginResponse])
async def login(req: LoginRequest, request: Request) -> Any:
    password = os.getenv("WEBUI_PASSWORD", "")
    client_ip = request.client.host if request.client else "unknown"

    # Rate limit check
    with _rate_limit_lock:
        _cleanup_expired_rate_limits()
        if client_ip in _rate_limit:
            fails, lock_until = _rate_limit[client_ip]
            if time.time() < lock_until:
                remaining = int(lock_until - time.time())
                raise HTTPException(429, f"登录尝试过多，请 {remaining} 秒后重试")

    if not password:
        if not _is_private_ip(client_ip):
            raise HTTPException(403, "Public access denied without password. Set WEBUI_PASSWORD in .env")
        token, expiry = _issue_token()
        return Envelope(data=LoginResponse(token=token, expires_at=expiry))

    if not hmac.compare_digest(req.password, password):
        with _rate_limit_lock:
            fails, lock_until = _rate_limit.get(client_ip, (0, 0))
            fails += 1
            if fails >= 5:
                _rate_limit[client_ip] = (fails, time.time() + 600)
            else:
                _rate_limit[client_ip] = (fails, lock_until)
            _rate_limit.move_to_end(client_ip)
            while len(_rate_limit) > _RATE_LIMIT_MAX_SIZE:
                _rate_limit.popitem(last=False)
        raise HTTPException(401, "Invalid password")

    # Success: reset rate limit
    with _rate_limit_lock:
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