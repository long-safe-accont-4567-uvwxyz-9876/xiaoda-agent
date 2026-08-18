from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger

# VULN-28：XFF 信任判定统一从 rate_limit 导入（规则单源，避免两处漂移）
from web.middleware.rate_limit import _peer_is_trusted_proxy, _trust_forwarded_for
from web.schemas import ChangePasswordRequest, Envelope, LoginRequest, LoginResponse, RecoverRequest

router = APIRouter(tags=["auth"])

_tokens: OrderedDict[str, float] = OrderedDict()
_TOKENS_MAX_SIZE = 1000
_rate_limit: OrderedDict[str, tuple[int, float]] = OrderedDict()
_RATE_LIMIT_MAX_SIZE = 1000

_SECRET: str = ""

# VULN-11: WebUI 实际监听地址。无密码免密模式必须结合 socket 绑定地址判定，
# 不能只看 client_ip（反代配置不当/直连场景下可被伪造）。缺省按非回环处理（fail-closed）。
_WEBUI_BIND_HOST: str = os.getenv("WEBUI_HOST", "") or os.getenv("WEBUI_BIND", "")

_secret_lock = Lock()
_revoked_lock = Lock()
_tokens_lock = Lock()
_rate_limit_lock = Lock()
# 已撤销 token 内存缓存，避免每次请求都读文件
_revoked_cache: set[str] = set()
_revoked_cache_mtime: float = 0.0
# 滑动续期宽限期（秒）：续期撤销旧 token 后，旧 token 在宽限期内仍视为有效，
# 避免并发请求 B 在 A 刚续期作废旧 token 的瞬间被误伤 401。
_RENEWAL_GRACE_SECONDS = 30.0
# token -> 宽限期截止时间（epoch 秒）。仅内存态，用于续期撤销的短窗口豁免。
_revoked_grace: dict[str, float] = {}

# VULN-29：媒体访问 cookie。前端用裸 <audio :src>/<img src> 引用 /media
# （无法携带 Authorization 头），登录成功时下发本 cookie（Path=/media，
# HttpOnly + SameSite=Strict），/media 中间件校验之，前端零改动。
MEDIA_COOKIE_NAME = "x_media_token"


def set_media_cookie(response: Any, token: str, expires_at: float) -> None:
    """在响应上下发 /media 访问 cookie（HttpOnly + SameSite=Strict + Path 限定）。

    response 为 None 时（直调/测试场景无响应对象）跳过。
    """
    if response is None:
        return
    response.set_cookie(
        MEDIA_COOKIE_NAME, token,
        path="/media",
        httponly=True,
        samesite="strict",
        max_age=int(max(0, expires_at - time.time())),
    )


def clear_media_cookie(response: Any) -> None:
    """登出/撤销时清除 /media 访问 cookie。response 为 None 时跳过。"""
    if response is None:
        return
    response.delete_cookie(MEDIA_COOKIE_NAME, path="/media")

_token_epoch: int | None = None
_token_epoch_lock = Lock()


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
            # VULN-10: 已存在的旧文件也要强制校正为 0600（旧版只在新建时 chmod）。
            with contextlib.suppress(OSError):
                secret_path.chmod(0o600)
        else:
            _SECRET = secrets.token_hex(32)
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(_SECRET, encoding="utf-8")
            with contextlib.suppress(OSError):
                secret_path.chmod(0o600)
        return _SECRET


def _get_revoked_path() -> Path:
    """黑名单文件路径。"""
    from config import get_credentials_dir
    return get_credentials_dir() / "revoked_tokens.json"


def _get_token_epoch_path() -> Path:
    from config import get_credentials_dir
    return get_credentials_dir() / "token_epoch"


def _load_token_epoch() -> int:
    global _token_epoch
    with _token_epoch_lock:
        if _token_epoch is None:
            path = _get_token_epoch_path()
            try:
                _token_epoch = int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0
            except (OSError, ValueError):
                _token_epoch = 0
        return _token_epoch


def _increment_token_epoch() -> int:
    global _token_epoch
    with _token_epoch_lock:
        current = _token_epoch
        if current is None:
            path = _get_token_epoch_path()
            try:
                current = int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0
            except (OSError, ValueError):
                current = 0
        _token_epoch = current + 1
        path = _get_token_epoch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(_token_epoch), encoding="utf-8")
        return _token_epoch


def _extract_expiry(token: str) -> float:
    """从 token 中提取过期时间。"""
    try:
        decoded = base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode()).decode()
        parts = decoded.rsplit(".", 3)
        if len(parts) == 4:
            return float(parts[0])
        legacy_parts = decoded.rsplit(".", 2)
        return float(legacy_parts[0]) if len(legacy_parts) == 3 else 0.0
    except Exception as exc:
        logger.debug("auth.extract_expiry_failed: {}", exc, exc_info=True)
        return 0.0


def _now() -> float:
    """当前时间（可注入时钟，便于测试宽限期边界）。"""
    return time.time()


def _revoke_token(token: str, grace_seconds: float = 0.0) -> None:
    """将 token 加入黑名单（持久化到文件）。

    grace_seconds > 0 时，token 在宽限期内仍视为有效（用于滑动续期撤销旧 token，
    避免并发请求在续期瞬间被误伤）；否则立即生效（logout / revoke-all 语义）。
    """
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
        # 宽限期：续期撤销记录截止时间；无宽限期则清除旧豁免
        if grace_seconds > 0:
            _revoked_grace[token] = _now() + grace_seconds
        else:
            _revoked_grace.pop(token, None)
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
            if token not in _revoked_cache:
                return False
            # 宽限期：刚因续期被撤销的旧 token，在宽限期内仍视为有效
            deadline = _revoked_grace.get(token)
            if deadline is not None:
                if _now() < deadline:
                    return False
                # 宽限期已过，惰性清理避免无限增长
                _revoked_grace.pop(token, None)
            return True
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
    epoch = _load_token_epoch()
    payload = f"{expiry}.{nonce}.{epoch}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode().rstrip("=")
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
        decoded = base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode()).decode()
        parts = decoded.rsplit(".", 3)
        if len(parts) != 4:
            return False
        expiry_str, nonce, epoch_str, sig = parts
        expiry = float(expiry_str)
        if expiry < time.time():
            return False
        if int(epoch_str) != _load_token_epoch():
            return False
        payload = f"{expiry_str}.{nonce}.{epoch_str}"
        expected_sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        # 检查黑名单
        if _is_revoked(token):
            return False
        with _tokens_lock:
            _tokens[token] = expiry
            _tokens.move_to_end(token)
            while len(_tokens) > _TOKENS_MAX_SIZE:
                _tokens.popitem(last=False)
        return True
    except Exception as exc:
        logger.debug("auth.validate_token_failed: {}", exc, exc_info=True)
        return False


def _is_private_ip(ip: str) -> bool:
    """判断 IP 是否为回环/内网/链路本地/保留地址。

    使用 ipaddress 标准库替代手写判断，覆盖：
    - RFC1918 私有地址（10/8、172.16/12、192.168/16）
    - 回环（127/8、::1）
    - 链路本地（169.254/16、fe80::/10）
    - CGNAT（100.64/10，Python < 3.13 的 is_private 不覆盖, 显式判断）
    - 多播、保留地址
    - IPv6 ULA（fc00::/7）
    修复 P1：原手写判断遗漏 CGNAT、169.254、IPv6 ULA 等。
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        addr = ipaddress.ip_address(ip)
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            return True
        # CGNAT 100.64.0.0/10: Python < 3.13 的 is_private 不覆盖, 显式判断
        if isinstance(addr, ipaddress.IPv4Address):
            if 0x64400000 <= int(addr) <= 0x647FFFFF:  # 100.64.0.0 - 100.127.255.255
                return True
        return False
    except ValueError:
        return False


def _is_loopback_bind() -> bool:
    """判断 WebUI 实际监听地址是否为回环地址。

    VULN-11 修复：无密码免密模式不能只看客户端 IP（反代/直连场景下可被伪造），
    还需结合实际 socket 绑定地址。仅当 WebUI 只监听回环地址（127.0.0.1 /
    localhost / ::1）时才允许免密，否则 fail-closed。
    """
    host = (_WEBUI_BIND_HOST or "").strip()
    if host in ("", "0.0.0.0", "::"):
        return False
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _get_client_ip(request: Request) -> str:
    """提取客户端真实 IP。

    默认使用 TCP 对端 ``request.client.host``。若部署在可信反代后且
    ``TRUST_FORWARDED_FOR`` 启用，则解析 ``X-Forwarded-For`` 头:
    取最右侧非可信代理 IP (覆盖多层反代场景, 跳过末尾的内网代理 IP).
    修复 P1：原代码用 request.client.host，反代后所有请求对端均为 127.0.0.1，
    导致无密码模式对公网开放、限流白名单失效。

    VULN-28：仅当 socket 对端是可信代理（回环/显式可信网段）时才解析 XFF ——
    攻击者直连时自己控制 XFF 头，无条件信任即可伪造来源 IP，绕过 per-IP 的
    登录失败锁定（5 次锁 600s）。
    """
    peer = request.client.host if request.client else "unknown"
    if _trust_forwarded_for() and _peer_is_trusted_proxy(peer):
        xff = request.headers.get("X-Forwarded-For", "") or request.headers.get("x-forwarded-for", "")
        if xff:
            # X-Forwarded-For: client, proxy1, proxy2
            # 取最右侧非内网/非可信代理的 IP, 避免攻击者伪造 XFF 前缀
            candidates = [ip.strip() for ip in xff.split(",") if ip.strip()]
            for ip in reversed(candidates):
                try:
                    addr = ipaddress.ip_address(ip)
                    if not (addr.is_private or addr.is_loopback):
                        return ip
                except ValueError:
                    continue
            # 全部都是内网 (如纯内网部署), 取最左侧 (原始客户端)
            if candidates:
                return candidates[0]
    return peer


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
        expiry = _extract_expiry(token)
        if expiry - time.time() < 86400:  # 不到1天就续
            new_token, new_expiry = _issue_token()
            _revoke_token(token, grace_seconds=_RENEWAL_GRACE_SECONDS)  # 旧 token 作废（带宽限期）
            request.state.new_token = new_token
            request.state.new_expiry = new_expiry
            logger.info("auth.token_renewed old_expiry={} new_expiry={}", int(expiry), int(new_expiry))
    except Exception as e:
        logger.debug("auth.renew_check_failed error={}", str(e))
    return "webui"


_load_or_create_secret()


def _cleanup_expired_rate_limits() -> None:
    """清理已过期的 rate limit 条目，防止 _rate_limit 无限增长。

    lock_until <= 0 表示"计数中、尚未锁定"（login/recover 失败计数的中间态），
    不能当作过期清理，否则失败计数永远停在 1，5 次锁定永不触发。
    """
    now = time.time()
    expired = [ip for ip, (_, lock_until) in _rate_limit.items()
               if lock_until > 0 and lock_until < now]
    for ip in expired:
        _rate_limit.pop(ip, None)


@router.post("/auth/login", response_model=Envelope[LoginResponse])
async def login(req: LoginRequest, request: Request, response: Response = None) -> Any:
    password = os.getenv("WEBUI_PASSWORD", "")
    client_ip = _get_client_ip(request)

    # Rate limit check
    with _rate_limit_lock:
        _cleanup_expired_rate_limits()
        if client_ip in _rate_limit:
            fails, lock_until = _rate_limit[client_ip]
            if time.time() < lock_until:
                remaining = int(lock_until - time.time())
                raise HTTPException(429, f"登录尝试过多，请 {remaining} 秒后重试")

    if not password:
        if not _is_private_ip(client_ip) or not _is_loopback_bind():
            raise HTTPException(403, "Public access denied without password. Set WEBUI_PASSWORD in .env")
        token, expiry = _issue_token()
        set_media_cookie(response, token, expiry)
        return Envelope(data=LoginResponse(token=token, expires_at=expiry))

    # VULN-28: 弱密码告警（登录即主人模型下 WEBUI_PASSWORD 是唯一信任边界，
    # 首次登录尝试即告警，无论成败）
    _warn_weak_password(password)

    if not hmac.compare_digest(req.password, password):
        # VULN-28: 失败审计（含来源 IP，供异常检测/事后追溯）
        logger.warning("auth.login_failed ip={} fails_next={}", client_ip,
                       _rate_limit.get(client_ip, (0, 0))[0] + 1)
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
    set_media_cookie(response, token, expiry)
    return Envelope(data=LoginResponse(token=token, expires_at=expiry))


_MIN_PASSWORD_LEN = 8
_warned_weak_password = False


def _warn_weak_password(password: str) -> None:
    """WEBUI_PASSWORD 过短时打 CRITICAL 告警（每个进程只告警一次）。"""
    global _warned_weak_password
    if _warned_weak_password or len(password) >= _MIN_PASSWORD_LEN:
        return
    _warned_weak_password = True
    logger.critical(
        "auth.weak_password len={} min_required={} "
        "WEBUI_PASSWORD 是唯一登录边界，弱密码可被暴力破解，请立即更换为 "
        "{} 位以上随机字符串",
        len(password), _MIN_PASSWORD_LEN, _MIN_PASSWORD_LEN,
    )


# ── 密码找回 & 修改密码 ──────────────────────────────────────


async def _audit_auth_event(request: Request, action: str, detail: str) -> None:
    """写入审计日志（参照现有 insert_audit_log 用法）。

    从 request.app.state.core 拿 core，拿不到（直调/测试场景）就跳过。
    """
    try:
        if request is None:
            return
        core = getattr(request.app.state, "core", None)
        if core is None or not hasattr(core, "db"):
            return
        await core.db.insert_audit_log(action, "webui", detail)
        await core.db.commit()
    except Exception as exc:
        logger.debug("auth.audit_log_failed error={}", str(exc))


def _update_env_password(new_password: str) -> None:
    """更新 .env 中的 WEBUI_PASSWORD（复用 setup_wizard 的 .env 读写机制，保持行序）。

    同时更新 os.environ，使进程内登录校验立即生效。失败时抛 RuntimeError 由调用方处理。
    """
    from setup_wizard import ENV_PATH, _load_env_values, _parse_env_lines, _write_env
    existing_lines = _parse_env_lines(ENV_PATH)
    current = _load_env_values()
    merged = dict(current)
    merged["WEBUI_PASSWORD"] = new_password
    _write_env(existing_lines, merged)
    os.environ["WEBUI_PASSWORD"] = new_password


def _check_recover_rate_limit(client_ip: str) -> None:
    """复用登录失败锁定桶：锁定期间一律 429。"""
    with _rate_limit_lock:
        _cleanup_expired_rate_limits()
        if client_ip in _rate_limit:
            _, lock_until = _rate_limit[client_ip]
            if time.time() < lock_until:
                remaining = int(lock_until - time.time())
                raise HTTPException(429, f"尝试次数过多，请 {remaining} 秒后重试")


def _record_recover_failure(client_ip: str) -> None:
    """复用 login 失败计数机制：5 次失败锁 600 秒。"""
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


@router.get("/auth/recover-question", response_model=Envelope[dict])
async def recover_question() -> Any:
    """返回已配置的找回问题（无鉴权，与 login 同级别）。"""
    from security.recovery_qa import get_question
    question = get_question()
    return Envelope(data={"question": question or "", "has_question": bool(question)})


@router.post("/auth/recover", response_model=Envelope[dict])
async def recover(req: RecoverRequest, request: Request, response: Response = None) -> Any:
    """通过找回问答重置密码（无鉴权）。

    成功后更新 .env、吊销全部 token（epoch 递增）、清除媒体 cookie，
    不返回 token —— 要求用户用新密码重新登录。
    """
    from security.recovery_qa import get_question, verify_answer

    client_ip = _get_client_ip(request)

    if not get_question():
        raise HTTPException(400, "未设置密码找回问题")

    _check_recover_rate_limit(client_ip)

    if len(req.new_password) < _MIN_PASSWORD_LEN:
        raise HTTPException(400, f"新密码至少需要 {_MIN_PASSWORD_LEN} 位")

    if not verify_answer(req.answer):
        logger.warning("auth.recover_failed ip={}", client_ip)
        _record_recover_failure(client_ip)
        raise HTTPException(403, "找回答案错误")

    try:
        _update_env_password(req.new_password)
    except Exception as exc:
        logger.error("auth.recover_env_update_failed error={}", str(exc))
        raise HTTPException(500, "更新密码配置失败") from None

    # 成功：清除该 IP 的失败计数
    with _rate_limit_lock:
        _rate_limit.pop(client_ip, None)

    _increment_token_epoch()  # 吊销全部 token
    clear_media_cookie(response)

    await _audit_auth_event(request, "webui.password.recovered", "password reset via recovery question")
    _warn_weak_password(req.new_password)
    logger.warning("auth.password_recovered ip={}", client_ip)
    return Envelope(data={"ok": True})


@router.post("/auth/change-password", response_model=Envelope[dict])
async def change_password(req: ChangePasswordRequest, user_id: str = Depends(get_current_user),
                          request: Request = None, response: Response = None) -> Any:
    """修改登录密码（需鉴权；修改必须通过找回答案验证）。

    成功后更新 .env、吊销全部 token 并签发新 token（旧 token 按滑动续期
    方式带宽限期撤销），可同时轮换找回问答。返回新 token 供前端替换本地存储。
    """
    from security.recovery_qa import set_recovery, verify_answer

    current_password = os.getenv("WEBUI_PASSWORD", "")
    client_ip = _get_client_ip(request) if request is not None else "unknown"

    # 当前密码校验：未设置过密码时允许空 old_password
    if current_password and not hmac.compare_digest(req.old_password or "", current_password):
        logger.warning("auth.change_password_old_mismatch ip={}", client_ip)
        raise HTTPException(403, "旧密码错误")

    # 需求硬约束：改密码必须通过验证问题
    if not verify_answer(req.answer):
        logger.warning("auth.change_password_answer_failed ip={}", client_ip)
        raise HTTPException(403, "找回答案错误")

    if len(req.new_password) < _MIN_PASSWORD_LEN:
        raise HTTPException(400, f"新密码至少需要 {_MIN_PASSWORD_LEN} 位")
    if req.new_password == current_password:
        raise HTTPException(400, "新密码不能与当前密码相同")

    # 可选：轮换找回问答（两者都非空才轮换）。
    # 顺序约束：必须等密码写入成功后再轮换问答，否则 .env 写入失败时会出现
    # "问答已轮换但密码未改"的不一致状态。
    new_question = (req.new_question or "").strip()
    new_answer = (req.new_answer or "").strip()

    try:
        _update_env_password(req.new_password)
    except Exception as exc:
        logger.error("auth.change_password_env_update_failed error={}", str(exc))
        raise HTTPException(500, "更新密码配置失败") from None

    # 密码写入成功后轮换问答（若请求携带新问答）
    if new_question and new_answer:
        try:
            set_recovery(new_question, new_answer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    _increment_token_epoch()  # 吊销全部旧 token
    new_token, expiry = _issue_token()

    # 旧 token 按滑动续期方式撤销（带宽限期），避免当前请求瞬间 401
    if request is not None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            old_token = auth_header[7:]
            _revoke_token(old_token, grace_seconds=_RENEWAL_GRACE_SECONDS)
            _tokens.pop(old_token, None)
        # get_current_user 可能已触发滑动续期并放入 request.state，
        # 清除之，避免中间件再下发一个已被 epoch 吊销的 token 头。
        if hasattr(request.state, "new_token"):
            delattr(request.state, "new_token")

    set_media_cookie(response, new_token, expiry)

    await _audit_auth_event(request, "webui.password.changed", "password changed")
    _warn_weak_password(req.new_password)
    logger.warning("auth.password_changed ip={}", client_ip)
    return Envelope(data={"token": new_token, "expires_at": expiry})


@router.post("/auth/logout", response_model=Envelope[None])
async def logout(user_id: str = Depends(get_current_user), request: Request = None,
                 response: Response = None) -> Any:
    """撤销当前 token（真正加入黑名单）。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        _revoke_token(token)
        _tokens.pop(token, None)
    clear_media_cookie(response)
    return Envelope(data=None)


@router.post("/auth/revoke-all", response_model=Envelope[None])
async def revoke_all(user_id: str = Depends(get_current_user),
                     response: Response = None) -> Any:
    """撤销所有 token（改密码后强制全量重新登录）。"""
    _increment_token_epoch()
    for token in list(_tokens.keys()):
        _revoke_token(token)
    _tokens.clear()
    clear_media_cookie(response)
    return Envelope(data=None)
