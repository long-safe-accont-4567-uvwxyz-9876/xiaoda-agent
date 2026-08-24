"""微信 Bot WebUI 管理路由

提供微信 iLink Bot 的扫码登录、连接测试、消息轮询启停、连接状态查询等
HTTP 端点，供 WebUI 前端调用。

端点总览：
  POST /wechat/qrcode          生成扫码登录二维码（需认证）
  GET  /wechat/qrcode-status   轮询扫码状态（需认证）
  POST /wechat/test            测试连接——发送测试消息（需认证）
  POST /wechat/start           启动消息轮询（需认证）
  POST /wechat/stop            停止消息轮询并清除凭证（需认证）
  GET  /wechat/status          连接状态查询（无需认证）

凭证文件：~/.ai-agent/wechat_credentials.json（由 wechat_bot_adapter 管理）
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger

from channel_adapter_base import upsert_env_file_line
from config import ENV_PATH
from ilink_client import ILinkClient
from web.routers.auth import get_current_user
from web.schemas import Envelope
from wechat_bot_adapter import clear_credentials, load_credentials, save_credentials

# 需认证的路由：所有端点默认走 get_current_user 依赖
router = APIRouter(tags=["wechat"], dependencies=[Depends(get_current_user)])
# 公开路由：仅 /wechat/status，供前端在未登录时探测连接状态
public_router = APIRouter(tags=["wechat"])

# 生命周期锁：串行化 start/stop 对 app.state.wechat_bot 的修改，
# 避免并发请求在 await 点交错导致旧 adapter 被误清、轮询任务失控。
# Minor#2（R3）：不能在 import 期创建 asyncio.Lock()——会绑定到 import 时的
# 事件循环，跨 loop 复用抛 "bound to a different event loop"（与适配器层
# _START_LOCK 的 M3 修复同因）。改为 per-loop 惰性创建。
_lifecycle_locks: "dict" = {}
_lifecycle_locks_guard = __import__("threading").Lock()


def _save_master_wechat_id(user_id: str) -> None:
    """扫码登录成功后，把微信 from_user_id 绑定为微信主人。

    写入 .env 的 MASTER_WECHAT_OPENID（持久化）+ 更新 os.environ，并刷新当前进程
    运行时 SecurityFilter.owner_ids，使主人识别即时生效（无需重启服务）。
    """
    try:
        existing = os.getenv("MASTER_WECHAT_OPENID", "").strip()
        ids = [x.strip() for x in existing.split(",") if x.strip()]
        if user_id in ids:
            return
        ids.append(user_id)
        value = ",".join(ids)
        upsert_env_file_line(Path(ENV_PATH), "MASTER_WECHAT_OPENID", value)
        os.environ["MASTER_WECHAT_OPENID"] = value
        logger.info("wechat.master_wechat_id_saved user_id={} total={}", user_id, len(ids))
        # 运行时即时生效
        try:
            from web.server import app as _web_app
            core = getattr(_web_app, "state", None) and getattr(_web_app.state, "core", None)
            if core is not None and getattr(core, "security", None) is not None:
                core.security.add_owner_id(user_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("wechat.refresh_runtime_owner_failed error={}", str(e)[:120])
    except Exception as e:  # noqa: BLE001
        logger.warning("wechat.save_master_wechat_id_failed error={}", str(e)[:200])


def _get_lifecycle_lock() -> asyncio.Lock:
    """返回绑定当前运行事件循环的生命周期锁（首次使用时惰性创建）。"""
    loop = asyncio.get_running_loop()
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _lifecycle_locks[loop] = lock
        return lock


def _build_adapter(request: Request) -> Any:
    """从 app.state.core 构造 WeChatBotAdapter 实例（不启动）。

    用于需要创建完整 adapter 实例的场景（如 /wechat/start 启动轮询）。
    凭证操作（save/load/clear）应直接使用模块级函数，无需创建 adapter。
    """
    from wechat_bot_adapter import WeChatBotAdapter
    core = request.app.state.core
    return WeChatBotAdapter(
        db=core.db,
        router=core.router,
        api=None,
        user_openid="",
        core=core,
    )


def _generate_qr_image_base64(data: str) -> str:
    """将文本/URL 转换为 base64 编码的 PNG 二维码图片。

    返回 data URI 格式，前端可直接用 <img src="..."> 显示。
    """
    import base64
    import io

    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------


@router.post("/wechat/qrcode", response_model=Envelope[dict])
async def generate_qrcode(request: Request) -> Any:
    """生成微信扫码登录二维码。

    创建无 bot_token 的 ILinkClient，调用 get_qrcode() 获取二维码。
    将 qrcode_url 转换为 base64 PNG 图片，前端可直接用 <img> 显示。
    将 qrcode_id 存储在 app.state 中供后续参考。
    """
    try:
        async with ILinkClient() as client:
            qr = await client.get_qrcode()
        qrcode_id = qr.get("qrcode_id", "")
        qrcode_url = qr.get("qrcode_url", "")
        if not qrcode_id or not qrcode_url:
            logger.error(
                "wechat.qrcode.empty_response has_id={} has_url={}",
                bool(qrcode_id), bool(qrcode_url),
            )
            return Envelope(
                ok=False,
                error={"code": "QRCODE_FAILED", "message": "上游未返回有效二维码，请稍后重试"},
            )
        # 存储在 app.state 中，供调试或后续逻辑参考
        request.app.state.wechat_qrcode_id = qrcode_id
        logger.info("wechat.qrcode.generated id={}", qrcode_id[:32])

        # 将扫码 URL 转换为 base64 PNG 图片，前端直接用 <img src="data:image/png;base64,...">
        qrcode_img = _generate_qr_image_base64(qrcode_url)

        return Envelope(data={
            "qrcode_id": qrcode_id,
            "qrcode_url": qrcode_url,
            "qrcode_img": qrcode_img,
        })
    except Exception as e:
        logger.error(
            "wechat.qrcode.generate_failed error={} type={}",
            str(e)[:200], type(e).__name__,
            exc_info=True,
        )
        return Envelope(
            ok=False,
            error={"code": "QRCODE_FAILED", "message": f"生成二维码失败: {e}"},
        )


@router.get("/wechat/qrcode-status", response_model=Envelope[dict])
async def get_qrcode_status(
    request: Request,
    qrcode_id: str = Query(..., description="get_qrcode 返回的二维码 ID"),
) -> Any:
    """轮询二维码扫码状态。

    status 取值：wait / scaned / confirmed / expired
    confirmed 时保存凭证到 ~/.ai-agent/wechat_credentials.json。
    """
    try:
        async with ILinkClient() as client:
            status_result = await client.get_qrcode_status(qrcode_id)
    except Exception as e:
        logger.error(
            "wechat.qrcode.status_failed id={} error={} type={}",
            qrcode_id[:32], str(e)[:200], type(e).__name__,
            exc_info=True,
        )
        return Envelope(
            ok=False,
            error={"code": "STATUS_FAILED", "message": f"查询扫码状态失败: {e}"},
        )

    st = status_result.get("status", "wait")
    logger.info("wechat.qrcode.status id={} status={}", qrcode_id[:32], st)

    if st == "confirmed":
        bot_token = status_result.get("bot_token", "")
        ilink_bot_id = status_result.get("ilink_bot_id", "")
        ilink_user_id = status_result.get("ilink_user_id", "")
        baseurl = status_result.get("baseurl", "")
        # 凭证不完整时不落盘，避免 /wechat/start 拿到空 token 却误报已连接
        if not bot_token or not ilink_user_id:
            logger.error(
                "wechat.qrcode.confirmed_incomplete has_token={} has_user={}",
                bool(bot_token), bool(ilink_user_id),
            )
            return Envelope(
                ok=False,
                error={"code": "INVALID_CREDENTIALS", "message": "登录返回的凭证不完整，请重新扫码"},
            )
        # 仅接受 HTTPS baseurl，非 HTTPS 用官方默认域名
        if baseurl and not baseurl.lower().startswith("https://"):
            logger.warning(
                "wechat.qrcode.rejected_non_https_baseurl url={}",
                baseurl[:200],
            )
            baseurl = ""
        # 保存凭证，供后续 /wechat/start 加载
        try:
            save_credentials(bot_token, ilink_bot_id, ilink_user_id, baseurl)
            # 扫码登录成功即绑定为微信主人：持久化 + 运行时即时生效
            _save_master_wechat_id(ilink_user_id)
        except Exception as e:
            logger.error(
                "wechat.qrcode.save_credentials_failed error={}",
                str(e)[:200], exc_info=True,
            )
            return Envelope(
                ok=False,
                error={"code": "SAVE_FAILED", "message": f"凭证保存失败: {e}"},
            )
        # W6：不回传 bot_token——敏感凭证已落盘，前端无需且不应持有明文 token。
        data: dict[str, Any] = {"status": st}
        if baseurl:
            data["baseurl"] = baseurl
        return Envelope(data=data)

    # wait / scaned / expired 等状态直接返回
    return Envelope(data={"status": st})


# ---------------------------------------------------------------------------
# 连接测试
# ---------------------------------------------------------------------------


@router.post("/wechat/test", response_model=Envelope[dict])
async def test_connection(request: Request) -> Any:
    """测试微信 Bot 连接——从凭证文件加载 bot_token 并发送测试消息。

    W9：统一失败协议——所有可预期失败（凭证缺失/无效/测试失败）一律返回
    data: { success: False, error: msg }（前端软失败分支），仅真正异常才用
    Envelope ok=False 包装，避免三种错误表达并存。

    M2：存在活跃运行中的 adapter 实例时，直接读其实时状态
    （is_connected/is_session_expired），绝不新建 ILinkClient / 发 getupdates——
    否则并发的空游标探测会窃取活跃 poller 的积压消息并竞争游标文件。
    仅当没有活跃实例（服务重启后）时，才回退到独立 verify_token 探测。
    """
    # M2：优先读活跃实例实时状态，避免与 live poller 并发 getupdates。
    try:
        import wechat_bot_adapter
        active = wechat_bot_adapter.get_active_bot()
    except Exception:
        active = None
    if active is not None and not active.is_closed():
        if active.is_session_expired:
            logger.info("wechat.test.active_instance_expired")
            return Envelope(
                data={"success": False, "error": "会话已过期，请重新扫码登录"},
            )
        if bool(active.is_connected):
            logger.info("wechat.test.active_instance_connected")
            return Envelope(data={"success": True})
        logger.info("wechat.test.active_instance_not_connected")
        return Envelope(
            data={"success": False, "error": "微信 Bot 未连接（正在重连或已断开）"},
        )

    # 加载凭证
    try:
        creds = load_credentials()
    except Exception as e:
        logger.error(
            "wechat.test.load_credentials_failed error={}",
            str(e)[:200], exc_info=True,
        )
        return Envelope(
            data={"success": False, "error": f"加载凭证失败: {e}"},
        )

    if not creds:
        return Envelope(
            data={"success": False, "error": "未找到微信凭证，请先扫码登录"},
        )

    bot_token = creds.get("bot_token", "")
    ilink_user_id = creds.get("ilink_user_id", "")
    if not bot_token or not ilink_user_id:
        return Envelope(
            data={"success": False, "error": "凭证不完整（缺少 bot_token 或 ilink_user_id）"},
        )

    # 用凭证中的 baseurl 初始化 ILinkClient（无 baseurl 或非 HTTPS 时走默认）
    baseurl = creds.get("baseurl", "")
    kwargs: dict[str, Any] = {"bot_token": bot_token}
    if baseurl and baseurl.lower().startswith("https://"):
        kwargs["base_url"] = baseurl

    try:
        async with ILinkClient(**kwargs) as client:
            ok, msg = await client.send_test_message(bot_token, ilink_user_id)
    except Exception as e:
        logger.error(
            "wechat.test.failed error={} type={}",
            str(e)[:200], type(e).__name__, exc_info=True,
        )
        return Envelope(data={"success": False, "error": str(e)[:200]})

    if ok:
        # session_expired：token 被识别但会话过期，不能算连接成功
        if msg == "session_expired":
            logger.warning(
                "wechat.test.session_expired user={}",
                ilink_user_id[:16],
            )
            return Envelope(
                data={"success": False, "error": "会话已过期，请重新扫码登录"},
            )
        logger.info("wechat.test.success user={}", ilink_user_id[:16])
        return Envelope(data={"success": True})
    logger.warning("wechat.test.failed user={} msg={}", ilink_user_id[:16], msg)
    return Envelope(data={"success": False, "error": msg})


# ---------------------------------------------------------------------------
# 消息轮询启停
# ---------------------------------------------------------------------------


@router.post("/wechat/start", response_model=Envelope[dict])
async def start_bot(request: Request) -> Any:
    """启动微信消息轮询。

    从凭证文件加载凭证，创建 WeChatBotAdapter 并调用 start()。
    start() 内部会加载凭证、初始化 ILinkClient、启动长轮询任务。

    T4（僵尸 adapter 修复）：start() 返回结构化 readiness dict
    {ok, connected, polling, error}；仅在 readiness.ok 时才把 adapter 挂载到
    app.state.wechat_bot——失败时如实返回失败详情，绝不挂载未就绪实例
    （旧实现无条件挂载并返回 success=True，产生 /wechat/stop 无法停止的僵尸）。
    """
    async with _get_lifecycle_lock():
        # 先停止已有的 bot 实例（避免重复轮询）
        existing = getattr(request.app.state, "wechat_bot", None)
        if existing is not None:
            try:
                await existing.stop()
            except Exception as e:
                logger.warning(
                    "wechat.start.stop_existing_failed error={}",
                    str(e)[:200],
                )
            request.app.state.wechat_bot = None

        try:
            adapter = _build_adapter(request)
        except Exception as e:
            logger.error(
                "wechat.start.build_adapter_failed error={}",
                str(e)[:200], exc_info=True,
            )
            return Envelope(
                ok=False,
                error={"code": "INIT_FAILED", "message": f"适配器初始化失败: {e}"},
            )

        # 预检凭证是否存在（start() 内部也会加载，但提前给出明确错误更友好）
        creds = load_credentials()
        if not creds:
            return Envelope(
                ok=False,
                error={"code": "NO_CREDENTIALS", "message": "未找到微信凭证，请先扫码登录"},
            )

        try:
            readiness = await adapter.start() or {}
        except Exception as e:
            logger.error(
                "wechat.start.failed error={} type={}",
                str(e)[:200], type(e).__name__, exc_info=True,
            )
            request.app.state.wechat_bot = None
            return Envelope(
                ok=False,
                error={"code": "START_FAILED", "message": f"启动失败: {e}"},
            )
        # T4：以结构化 readiness 判定成败；失败不挂载、如实返回详情
        if not readiness.get("ok"):
            detail = readiness.get("error") or "适配器未就绪（connected/polling 未达成）"
            logger.warning(
                "wechat.start.not_ready connected={} polling={} error={}",
                bool(readiness.get("connected")), bool(readiness.get("polling")),
                str(detail)[:200],
            )
            request.app.state.wechat_bot = None
            return Envelope(
                ok=False,
                error={"code": "START_FAILED", "message": f"启动失败: {detail}"},
            )
        request.app.state.wechat_bot = adapter
        logger.info("wechat.bot.started")
        return Envelope(data={"success": True})


@router.post("/wechat/stop", response_model=Envelope[dict])
async def stop_bot(request: Request) -> Any:
    """停止微信消息轮询并清除凭证文件。"""
    async with _get_lifecycle_lock():
        bot = getattr(request.app.state, "wechat_bot", None)
        success = True

        if bot is not None:
            try:
                await bot.stop()
                logger.info("wechat.bot.stopped")
            except Exception as e:
                logger.error(
                    "wechat.stop.failed error={}",
                    str(e)[:200], exc_info=True,
                )
                success = False
            request.app.state.wechat_bot = None
        else:
            logger.info("wechat.stop.no_active_bot")

        # 清除凭证文件（无论 bot 是否存在都清除，确保登出干净）
        try:
            clear_credentials()
        except Exception as e:
            logger.warning(
                "wechat.credentials.clear_failed error={}",
                str(e)[:200],
            )
            # 凭证清除失败不改变 success（bot 已停止是主要操作）

        return Envelope(data={"success": success})


# ---------------------------------------------------------------------------
# 连接状态（无需认证）
# ---------------------------------------------------------------------------


@public_router.get("/wechat/status", response_model=Envelope[dict])
async def get_wechat_status() -> Any:
    """微信 Bot 连接状态查询（无需认证）。

    供前端在未登录时也能探测微信 Bot 是否在线。
    返回 { connected: bool, expired: bool, init_failed: bool }：
      - connected=True：bot 活跃且真实连接（is_connected），或凭证存在可启动
      - expired=True：bot 存在但会话已确认过期（需重新扫码）
      - init_failed=True：AgentCore 初始化失败（故障可见）

    W8：口径统一——活跃实例以 _connected 网络状态为准（会话过期重试期间
    显示未连接而非误报在线）；凭证 fallback 仅用于无活跃实例的场景
    （服务重启后），避免覆盖活跃实例的过期/断开状态。
    """
    connected = False
    expired = False
    init_failed = False
    bot = None
    try:
        import wechat_bot_adapter
        bot = wechat_bot_adapter.get_active_bot()
        if bot is not None and not bot.is_closed():
            if bot.is_session_expired:
                expired = True
            else:
                connected = bool(bot.is_connected)
            init_failed = bool(bot.has_init_failed)
    except Exception:
        logger.warning("wechat.status.active_bot_probe_failed", exc_info=True)
    # 无活跃实例（轮询未启动或服务重启后）：凭证存在即视为已登录（可一键启动），
    # 避免刷新后状态丢失；仅当没有任何 bot 实例时才 fallback。
    if bot is None and not connected and not expired:
        try:
            if load_credentials():
                connected = True
        except Exception:
            logger.warning("wechat.status.credential_fallback_failed", exc_info=True)
    return Envelope(data={
        "connected": connected,
        "expired": expired,
        "init_failed": init_failed,
    })
