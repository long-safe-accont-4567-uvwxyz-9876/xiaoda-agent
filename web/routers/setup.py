from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from config import get_base_url_for_provider
from web.routers.auth import _get_client_ip, _is_private_ip, get_current_user
from web.schemas import Envelope

# test-key 速率限制：每 IP 最多 10 次/分钟（按 IP 分桶，全局窗口会让
# 多用户互相挤占配额）
_test_key_timestamps: dict[str, list[float]] = {}
_TEST_KEY_RATE_LIMIT = 10
_TEST_KEY_RATE_WINDOW = 60.0
_TEST_KEY_MAX_TRACKED_IPS = 256  # 桶数上限，防伪造 IP 撑爆内存
_test_key_lock = asyncio.Lock()


def _get_local_now():
    """获取本地时间（使用显式时区，修复 Windows/Docker 中系统时区不正确的问题）。"""
    from datetime import datetime
    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError, OSError):
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(tz)


# 模块级后台任务列表，防止 asyncio.Task 被 GC 回收
_reinit_tasks: list = []


# 免责协议全文（模块级常量，供前端通过 API 获取）
DISCLAIMER_TEXT = """本 Agent 由小妲的老父亲-"飞"个人学习用途二创开发，禁止用户生成任何违禁内容，禁止用于任何商业用途，否则一切后果与开发者无关，由用户一人承担。

免责声明

本项目是一个非官方的二次创作，不是原作的续作、衍生品或官方合作项目，与原作权利方没有任何隶属、授权或赞助关系。

项目中用到的角色名称、形象、语音、表情素材等知识产权归原版权方所有，代码仅供个人学习研究，不用于商业目的。表情素材来自社区公开资源，如有不妥请联系我，我会立即处理。

本项目基于 MIT 协议开源，第三方素材的版权和许可以各自原始项目为准。

使用本软件生成的内容由用户自行承担风险——AI 会犯错，请自行核实。第三方 API 服务的可用性和隐私政策由对应服务商负责。

如有任何问题或建议，欢迎 GitHub Issues 反馈。"""


def _mask_key_value(val: str) -> str:
    """脱敏：显示前4位和后4位，中间用 ***...*** 代替；空值返回空字符串。

    过短的值（<=8）仅显示首字符，避免泄露过多内容。
    """
    if not val:
        return ""
    if len(val) <= 8:
        return val[:1] + "****"
    return val[:4] + "***...***" + val[-4:]


def _require_local_source(request: Request) -> None:
    """引导期免认证的补充约束：仅允许私网/回环来源访问引导端点。

    防止公网部署未完成配置时，引导端点（覆写 .env、读取用户 PII）被公网
    未授权访问。注意：受 TRUST_FORWARDED_FOR 影响，反代场景下若未信任 XFF，
    此检查退化为对反代对端（通常 127.0.0.1）的校验，无法识别真实公网客户端；
    该场景的安全仍依赖 WEBUI_PASSWORD 已设置（见 auth.login 的公网拒绝逻辑）。
    """
    client_ip = _get_client_ip(request)
    if not _is_private_ip(client_ip):
        raise HTTPException(
            403,
            "Setup endpoints require local/private network access until configuration is complete",
        )


async def _is_first_run_or_authenticated(request: Request) -> str:
    """认证依赖：首次运行（.env 不存在或任一必填 key 为空）时允许无认证访问；
    非首次运行时必须携带有效 Bearer Token。返回用户标识。

    安全策略：fail-closed。若 is_first_run() 因文件锁、导入错误、.env 解析
    异常等任何原因抛错，一律要求认证，避免攻击者通过制造异常绕过认证调用
    /setup/keys 等敏感端点覆写 .env。
    """
    try:
        from setup_wizard import is_first_run
        first_run = is_first_run()
    except (OSError, KeyError, ValueError, RuntimeError, ImportError) as e:
        logger.error("setup.first_run_check_failed error={} -> deny", str(e))
        raise HTTPException(
            status_code=503,
            detail="Setup availability check failed. Configure .env manually or contact admin."
        ) from None
    except Exception:
        logger.exception("setup._is_first_run_or_authenticated.unexpected_error")
        raise HTTPException(
            status_code=503,
            detail="Setup availability check failed. Configure .env manually or contact admin."
        ) from None
    if first_run:
        _require_local_source(request)
        return "setup"
    return await get_current_user(request)


def _is_profile_done() -> bool:
    """用户资料是否已完成（USER.md 中称呼与姓名均已实际填写）。

    与 ``get_first_run`` 的 profile_done 判定保持一致，供认证依赖复用，
    避免两处判定漂移（CodeRabbit 关注点）。
    """
    try:
        import re as _re_local

        from config import WORKSPACE_DIR
        user_md = WORKSPACE_DIR / "USER.md"
        if not user_md.exists():
            return False
        content = user_md.read_text(encoding="utf-8-sig")
        addr = _re_local.search(r'-\s*称呼[：:]\s*(.+)', content)
        name = _re_local.search(r'-\s*姓名[：:]\s*(.+)', content)
        if addr and name:
            addr_val = addr.group(1).strip()
            name_val = name.group(1).strip()
            if addr_val and not addr_val.startswith("（") and name_val and not name_val.startswith("（"):
                return True
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("setup.profile_done_check_failed: {}", exc, exc_info=True)
    except Exception:
        logger.exception("setup._is_profile_done.unexpected_error")
    return False


async def _profile_endpoint_access(request: Request) -> str:
    """认证依赖：用户资料页端点。

    向导未完成（首次运行 或 用户资料未完成）时允许无认证访问，
    向导完成后必须携带有效 Bearer Token。

    根治 profileSave 401：用户保存必填 key 后 ``is_first_run()`` 立即变
    False，但前端仍停留在向导的 profile 步骤；若此时要求 token，设置了
    WEBUI_PASSWORD 的机器上 ``login('')`` 拿不到 token（浏览器也无有效
    token），保存资料必然 401。资料页只读写 USER.md（非敏感），故在
    profile 完成前保持免认证，与"向导流程完整性"语义一致。
    """
    try:
        from setup_wizard import is_first_run
        first_run = is_first_run()
    except (OSError, KeyError, ValueError, RuntimeError, ImportError) as e:
        logger.error("setup.profile_auth_check_failed error={} -> deny", str(e))
        raise HTTPException(
            status_code=503,
            detail="Setup availability check failed. Configure .env manually or contact admin."
        ) from None
    except Exception:
        logger.exception("setup._profile_endpoint_access.unexpected_error")
        raise HTTPException(
            status_code=503,
            detail="Setup availability check failed. Configure .env manually or contact admin."
        ) from None
    if first_run:
        _require_local_source(request)
        return "setup"
    if not _is_profile_done():
        _require_local_source(request)
        return "setup"
    return await get_current_user(request)


router = APIRouter(tags=["setup"])

# 需要认证的端点共享的依赖列表（首次运行时免认证）
_AUTH_DEPS = [Depends(_is_first_run_or_authenticated)]

# 用户资料端点专用依赖：向导未完成（首次运行或资料未完成）时免认证
_PROFILE_DEPS = [Depends(_profile_endpoint_access)]

_FALLBACK_REQUIRED_KEYS = (
    "MIMO_API_KEY",
    "QQBOT_APP_ID",
    "QQBOT_APP_SECRET",
    "SILICONFLOW_API_KEY",
)


def _check_first_run_from_env() -> bool:
    """从 .env 文件直接读取必填 key，判断是否首次运行（setup_wizard 导入失败时的降级路径）。"""
    import sys
    if getattr(sys, 'frozen', False):
        env_dir = os.path.dirname(sys.executable)
    else:
        env_dir = os.path.dirname(os.path.abspath(__file__))
        for _ in range(3):
            env_dir = os.path.dirname(env_dir)
    env_path = os.path.join(env_dir, ".env")
    if not os.path.exists(env_path):
        return True
    try:
        configured = set()
        with open(env_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                for k in _FALLBACK_REQUIRED_KEYS:
                    if line.startswith(k + "="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        if val:
                            configured.add(k)
        return not (set(_FALLBACK_REQUIRED_KEYS) <= configured)
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("setup.env_read_failed: {}", exc, exc_info=True)
        return True
    except Exception:
        logger.exception("setup.env_read_failed unexpected_error")
        return False


@router.get("/setup/first-run", response_model=Envelope[dict])
async def get_first_run() -> Any:
    """检测是否首次运行（.env 不存在或任一必填 key 为空），
    以及用户资料是否已配置。"""
    first_run = True
    try:
        from setup_wizard import is_first_run
        first_run = is_first_run()
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.error("setup.first_run_import_failed error={}", str(e))
        # .env 同步读下放线程池（全仓 asyncio.to_thread 惯例）
        first_run = await asyncio.to_thread(_check_first_run_from_env)
    except Exception:
        logger.exception("setup.get_first_run.unexpected_error")

    profile_done = _is_profile_done()
    logger.info("setup.first_run_result first_run={} profile_done={}", first_run, profile_done)
    return Envelope(data={"first_run": first_run, "profile_done": profile_done})


@router.get("/setup/version", response_model=Envelope[dict])
async def get_version() -> Any:
    """获取安装包版本号（无需认证）"""
    from web.routers.system import _read_version
    return Envelope(data={"version": _read_version()})


_FALLBACK_REQUIRED_KEYS_META = [
    {"key": "MIMO_API_KEY", "label": "MiMo API 密钥", "desc": "小米 MiMo 大模型 API 密钥", "url": "https://platform.xiaomimimo.com?ref=SU5WDZ", "url_desc": "注册 → 控制台 → API Keys"},
    {"key": "QQBOT_APP_ID", "label": "QQ Bot App ID", "desc": "QQ 机器人应用 ID", "url": "https://q.qq.com", "url_desc": "创建机器人应用 → 获取 AppID"},
    {"key": "QQBOT_APP_SECRET", "label": "QQ Bot App Secret", "desc": "QQ 机器人应用密钥", "url": "https://q.qq.com", "url_desc": "同一页面的 AppSecret"},
    {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow API 密钥", "desc": "硅基流动 API 密钥", "url": "https://cloud.siliconflow.cn/i/iM5RmeWc", "url_desc": "注册 → API Keys"},
]

_FALLBACK_OPTIONAL_KEYS_META = [
    {"key": "WEBUI_PASSWORD", "label": "Web UI 密码", "desc": "留空则无需密码登录", "url": "", "url_desc": ""},
    {"key": "TAVILY_API_KEY", "label": "Tavily 搜索 API 密钥", "desc": "AI 搜索引擎", "url": "https://tavily.com", "url_desc": "注册 → API Keys"},
    {"key": "ANYSEARCH_API_KEY", "label": "AnySearch 统一搜索密钥", "desc": "统一搜索基础设施（选填，搜索首选引擎，失败自动回退）", "url": "https://www.coze.cn/s/qBK5eb8QVoE/", "url_desc": "使用手册（含 Key 获取方式）"},
    {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API 密钥", "desc": "DeepSeek 大模型 API 密钥", "url": "https://platform.deepseek.com", "url_desc": "注册 → API Keys"},
    {"key": "OPENROUTER_API_KEY", "label": "OpenRouter API 密钥", "desc": "OpenRouter API 密钥", "url": "https://openrouter.ai", "url_desc": "注册 → API Keys"},
    {"key": "WOLFRAMALPHA_API_KEY", "label": "WolframAlpha 知识计算密钥", "desc": "知识计算引擎", "url": "https://products.wolframalpha.com/api/", "url_desc": "注册 → Get AppID"},
    {"key": "AGNES_API_KEY", "label": "Agnes AI 图像/视频密钥", "desc": "图片生成和视频生成的核心依赖", "url": "https://agnes-ai.cn", "url_desc": "注册 → API Keys"},
    {"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "GitHub 个人访问令牌", "desc": "GitHub MCP Server 所需", "url": "https://github.com/settings/tokens", "url_desc": "Generate new token"},
    {"key": "MODELSCOPE_ACCESS_TOKEN", "label": "魔搭 Access Token", "desc": "魔搭 ModelScope 免费模型发现", "url": "https://modelscope.cn", "url_desc": "注册 → 个人中心 → 访问令牌"},
]


# ── Key 探针库拆分（2026-08-22 P1）：实现见 setup_key_probes.py，此处 re-export 保持兼容 ──
from web.routers.setup_key_probes import (  # noqa: F401
    _test_agnes,
    _test_anysearch,
    _test_deepseek,
    _test_get_with_bearer,
    _test_github,
    _test_key_by_catalog,
    _test_key_by_name,
    _test_llama_cpp,
    _test_mimo,
    _test_modelscope,
    _test_ollama,
    _test_openrouter,
    _test_qqbot,
    _test_siliconflow,
    _test_siliconflow_embed,
    _test_tavily,
    _test_wolframalpha,
    test_single_key,
)


def _build_key_list(items: list[dict], current: dict[str, str], required: bool) -> list[dict[str, Any]]:
    """将 key 元数据列表组装为带脱敏值和配置状态的响应列表。"""
    result: list[dict[str, Any]] = []
    for item in items:
        key = item["key"]
        val = current.get(key, "")
        result.append({
            "key": key,
            "label": item["label"],
            "desc": item["desc"],
            "url": item.get("url", ""),
            "url_desc": item.get("url_desc", ""),
            "required": required,
            "configured": bool(val.strip()),
            "masked_value": _mask_key_value(val),
        })
    return result


def _load_key_definitions() -> tuple[list[dict], list[dict], Any]:
    """加载 key 定义（优先从 setup_wizard，降级到硬编码列表）。"""
    try:
        from setup_wizard import OPTIONAL_KEYS, REQUIRED_KEYS, _load_env_values
        logger.info("setup.keys.import_ok")
        return REQUIRED_KEYS, OPTIONAL_KEYS, _load_env_values
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.error("setup.keys.import_failed error={}", str(e))
        return _FALLBACK_REQUIRED_KEYS_META, _FALLBACK_OPTIONAL_KEYS_META, dict
    except Exception:
        logger.exception("setup.get_keys.unexpected_error")
        return _FALLBACK_REQUIRED_KEYS_META, _FALLBACK_OPTIONAL_KEYS_META, dict


@router.get("/setup/keys", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def get_keys() -> Any:
    """返回所有 Key 的配置状态（脱敏）。"""
    import sys
    logger.info("setup.keys.called frozen={} exe={}", getattr(sys, 'frozen', False), getattr(sys, 'executable', 'N/A'))

    REQUIRED_KEYS, OPTIONAL_KEYS, load_fn = _load_key_definitions()

    try:
        current = load_fn()
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.error("setup.keys.load_env_failed error={}", str(e))
        current = {}
    except Exception:
        logger.exception("setup.get_keys.unexpected_error")
        current = {}

    keys = _build_key_list(REQUIRED_KEYS, current, required=True)
    keys.extend(_build_key_list(OPTIONAL_KEYS, current, required=False))
    return Envelope(data={"keys": keys})


_TIMEOUT = 10.0



@router.post("/setup/test-key", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def test_key(body: dict, request: Request) -> Any:
    """测试 API Key 是否有效。"""
    import time
    # 按 IP 分桶限流（client_ip 此前是死赋值，全局窗口会让多用户互相挤占配额）
    client_ip = _get_client_ip(request)
    now = time.monotonic()
    async with _test_key_lock:
        recent = [t for t in _test_key_timestamps.get(client_ip, [])
                  if now - t < _TEST_KEY_RATE_WINDOW]
        if recent:
            _test_key_timestamps[client_ip] = recent
        else:
            _test_key_timestamps.pop(client_ip, None)
            while len(_test_key_timestamps) >= _TEST_KEY_MAX_TRACKED_IPS:
                _test_key_timestamps.pop(next(iter(_test_key_timestamps)))
        if len(recent) >= _TEST_KEY_RATE_LIMIT:
            return Envelope(ok=False, error={"code": "RATE_LIMITED", "message": "测试频率过高，请稍后再试"})
        _test_key_timestamps.setdefault(client_ip, []).append(now)

    key_name = body.get("key_name", "")
    key_value = body.get("key_value", "")

    if not key_name or not key_value:
        return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 key_name 和 key_value"})

    extra = body.get("extra", {})
    success, message = await test_single_key(key_name, key_value, extra)

    return Envelope(data={"success": success, "message": message})


def _validate_webui_password(body: dict, updates: dict) -> tuple[str, str, str]:
    """校验 WebUI 密码和找回问答；合法时将密码并入 updates 并返回三个字段。"""
    webui_password = str(body.get("webui_password") or "").strip()
    recovery_question = str(body.get("recovery_question") or "").strip()
    recovery_answer = str(body.get("recovery_answer") or "").strip()
    if not webui_password:
        return webui_password, recovery_question, recovery_answer
    if len(webui_password) < 8:
        raise HTTPException(400, detail={
            "code": "WEAK_PASSWORD",
            "message": "WebUI 密码至少需要 8 位",
        })
    if not recovery_question or not recovery_answer:
        raise HTTPException(400, detail={
            "code": "RECOVERY_REQUIRED",
            "message": "密码与找回问题必须同时设置",
        })
    if len(recovery_question) > 200:
        raise HTTPException(400, detail={
            "code": "RECOVERY_INVALID",
            "message": "找回问题长度需在 1~200 个字符之间",
        })
    if len(recovery_answer) < 2:
        raise HTTPException(400, detail={
            "code": "RECOVERY_INVALID",
            "message": "找回答案至少需要 2 个字符",
        })
    updates["WEBUI_PASSWORD"] = webui_password
    return webui_password, recovery_question, recovery_answer


def _save_recovery_qa(webui_password: str, question: str, answer: str) -> None:
    """保存找回问答（仅当本次设置了密码时；写入失败由 recovery_qa 内部降级）。"""
    if not webui_password:
        return
    try:
        from security.recovery_qa import set_recovery
        set_recovery(question, answer)
    except ValueError as exc:
        raise HTTPException(400, detail={
            "code": "RECOVERY_INVALID", "message": str(exc),
        }) from None


_QQ_CRED_KEYS = ("QQBOT_APP_ID", "QQBOT_APP_SECRET", "ENABLE_QQ_BOT")


def _detect_qq_credential_change(updates: dict, old_vals: dict[str, str]) -> bool:
    """判断 QQ 凭证是否实际变更（相同值重提交不算变更）。"""
    return any(
        k in updates and updates[k].strip() != old_vals[k].strip()
        for k in _QQ_CRED_KEYS
    )


@router.post("/setup/keys", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def save_keys(body: dict) -> Any:
    """将提供的 Key-Value 写入 .env 文件。"""
    try:
        from setup_wizard import (
            ENV_EXAMPLE_PATH,
            ENV_PATH,
            REQUIRED_KEYS,
            _load_env_values,
            _parse_env_lines,
            _write_env,
        )

        updates = body.get("keys")
        if not updates or not isinstance(updates, dict):
            return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 keys 字段（dict）"})

        updates = dict(updates)
        webui_password, recovery_question, recovery_answer = _validate_webui_password(body, updates)

        test_required = body.get("test_required", False)
        if test_required:
            test_error = await _test_required_keys(updates, REQUIRED_KEYS)
            if test_error is not None:
                return test_error

        _qq_old = {k: os.getenv(k, "") for k in _QQ_CRED_KEYS}

        provider_snapshots = await _auto_register_providers(updates)
        try:
            # .env 同步写下放线程池（全仓 asyncio.to_thread 惯例）
            await asyncio.to_thread(_write_env_file, updates, ENV_PATH, ENV_EXAMPLE_PATH, _parse_env_lines, _load_env_values, _write_env)
        except (OSError, ValueError, RuntimeError):
            await _rollback_auto_registered_providers(provider_snapshots)
            raise
        logger.info("setup.keys_saved count={}", len(updates))

        _save_recovery_qa(webui_password, recovery_question, recovery_answer)

        await _reload_env_and_cache(updates, ENV_PATH)
        _reset_credential_pool(updates)
        _update_config_and_refresh_clients(updates)

        _qq_changed = _detect_qq_credential_change(updates, _qq_old)
        _reinit_tasks.append(asyncio.create_task(_reinit_and_maybe_restart_qq(_qq_changed)))

        return Envelope(data={"saved": list(updates.keys()), "need_restart": False})
    except HTTPException:
        raise
    except (OSError, ValueError, RuntimeError, KeyError) as e:
        import traceback
        logger.error("setup.keys_save_failed error={} traceback={}", str(e), traceback.format_exc())
        return Envelope(ok=False, error={"code": "SAVE_FAILED", "message": f"保存失败: {str(e)}"})
    except Exception as e:
        logger.exception("setup.save_keys.unexpected_error")
        return Envelope(ok=False, error={"code": "SAVE_FAILED", "message": f"保存失败: {str(e)}"})


async def _test_required_keys(updates: Any, REQUIRED_KEYS: Any) -> Envelope | None:
    """对必填 Key 逐一测试。返回错误 Envelope 或 None（全部通过）。"""
    failed: list[dict[str, str]] = []
    required_key_names = [item["key"] for item in REQUIRED_KEYS]
    for rk in required_key_names:
        rv = updates.get(rk, "").strip()
        if not rv:
            continue
        # QQBOT_APP_ID 和 QQBOT_APP_SECRET 需要一起测试
        extra = {}
        if rk == "QQBOT_APP_ID":
            extra["QQBOT_APP_SECRET"] = updates.get("QQBOT_APP_SECRET", "")
        elif rk == "QQBOT_APP_SECRET":
            extra["QQBOT_APP_ID"] = updates.get("QQBOT_APP_ID", "")
        success, message = await test_single_key(rk, rv, extra)
        if not success:
            failed.append({"key": rk, "message": message})
    # QQBOT 组合测试去重
    seen_qqbot = False
    deduped_failed: list[dict[str, str]] = []
    for f in failed:
        if f["key"] in ("QQBOT_APP_ID", "QQBOT_APP_SECRET"):
            if not seen_qqbot:
                deduped_failed.append({"key": "QQBOT_APP_ID + QQBOT_APP_SECRET", "message": f["message"]})
                seen_qqbot = True
        else:
            deduped_failed.append(f)
    if deduped_failed:
        return Envelope(ok=False, error={
            "code": "KEY_TEST_FAILED", "message": "必填 Key 验证失败，未保存",
            "failed_keys": deduped_failed,
        })
    return None


def _write_env_file(updates: Any, ENV_PATH: Any, ENV_EXAMPLE_PATH: Any, _parse_env_lines: Any, _load_env_values: Any, _write_env: Any) -> None:
    """写入 .env 文件（不存在则从 .env.example 复制）。"""
    import os
    if not os.path.exists(ENV_PATH):
        if os.path.exists(ENV_EXAMPLE_PATH):
            shutil.copy2(ENV_EXAMPLE_PATH, ENV_PATH)
            with contextlib.suppress(OSError):
                os.chmod(ENV_PATH, 0o600)
            logger.info("setup.copied_env_example")
        else:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write("")
            with contextlib.suppress(OSError):
                os.chmod(ENV_PATH, 0o600)
            logger.info("setup.created_empty_env")

    existing_lines = _parse_env_lines(ENV_PATH)
    current = _load_env_values()
    merged = dict(current)
    merged.update(updates)

    # SiliconFlow Key 双向同步
    embed_key = merged.get("EMBED_API_KEY", "").strip()
    sf_key = merged.get("SILICONFLOW_API_KEY", "").strip()
    if embed_key and not sf_key:
        merged["SILICONFLOW_API_KEY"] = embed_key
        logger.info("setup.siliconflow_key_synced direction=embed→sf")
    elif sf_key and not embed_key:
        merged["EMBED_API_KEY"] = sf_key
        logger.info("setup.siliconflow_key_synced direction=sf→embed")

    _write_env(existing_lines, merged)


async def _reload_env_and_cache(updates: Any, ENV_PATH: Any) -> None:
    """重新加载环境变量、清除模型发现缓存。"""
    import os

    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    # 兜底：直接写入 os.environ
    for k, v in updates.items():
        vs = v.strip() if isinstance(v, str) else ""
        if vs:
            os.environ[k] = vs
    # 清除模型发现缓存
    try:
        from web._discovery_cache import invalidate_discovery_cache
        await invalidate_discovery_cache()
        logger.info("setup.discovery_cache_invalidated")
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("setup.discovery_cache_invalidate_failed error={}", str(e))
    except Exception:
        logger.exception("setup._reload_env_and_cache.unexpected_error")


def _reset_credential_pool(updates: Any) -> None:
    """重置凭证池中所有 DEAD 凭证，并替换为新 Key。"""
    try:
        from config_providers import get_provider_env_prefix
        from utils.credential_pool import Credential, get_credential_pool

        pool = get_credential_pool()
        # base_url 单一来源：provider catalog（agnes 特例：池内凭证不带 base_url，
        # 默认端点由 agnes_transport 自管，故保留空串——与原实现一致）
        _PROVIDER_KEY_MAP = {
            **{
                f"{get_provider_env_prefix(pid)}_API_KEY": (
                    pid, get_base_url_for_provider(pid).rstrip("/")
                )
                for pid in ("mimo", "siliconflow", "openrouter", "deepseek")
            },
            "AGNES_API_KEY": ("agnes", ""),
        }
        for env_key, (provider, base_url) in _PROVIDER_KEY_MAP.items():
            new_key = updates.get(env_key, "").strip()
            if not new_key:
                pool.reset_provider(provider)
                continue
            pool.replace_provider(provider, Credential(
                api_key=new_key, provider=provider, base_url=base_url,
            ))
        from config import get_provider_catalog

        catalog = get_provider_catalog()
        modelscope_credential = catalog.resolve_environment_alias("modelscope", updates)
        if modelscope_credential:
            pool.replace_provider("modelscope", Credential(
                api_key=modelscope_credential[1],
                provider="modelscope",
                base_url=catalog.get("modelscope").endpoint.base_url,
            ))
        else:
            pool.reset_provider("modelscope")
        logger.info("setup.credential_pool_updated")
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("setup.credential_pool_reset_failed error={}", str(e))
    except Exception:
        logger.exception("setup._reset_credential_pool.unexpected_error")


def _update_config_and_refresh_clients(updates: Any) -> None:
    """更新 config 模块变量并刷新 router/TTS/子 Agent 客户端。"""
    import os

    import config
    from utils.encrypted_credential import protect_credential
    config.MIMO_API_KEY = protect_credential(updates.get("MIMO_API_KEY", os.getenv("MIMO_API_KEY", "")))
    config.DEEPSEEK_API_KEY = updates.get("DEEPSEEK_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
    config.AGNES_API_KEY = updates.get("AGNES_API_KEY", os.getenv("AGNES_API_KEY", ""))

    # 重建 ModelRouter 的 MiMo/Agnes 客户端
    try:
        from web.app_ref import get_app
        app = get_app()
        if hasattr(app, "state") and hasattr(app.state, "core"):
            core = app.state.core
            router_obj = getattr(core, "router", None)
            if router_obj and hasattr(router_obj, "refresh_client"):
                router_obj.refresh_client()
                logger.info("setup.router_client_refreshed")
            tts_engine = getattr(core, "tts", None) or getattr(core, "tts_engine", None)
            if tts_engine and hasattr(tts_engine, "refresh_client"):
                tts_engine.refresh_client()
                logger.info("setup.tts_client_refreshed")
            dispatcher = getattr(core, "dispatcher", None)
            if dispatcher and hasattr(dispatcher, "refresh_all_clients"):
                n = dispatcher.refresh_all_clients()
                logger.info("setup.sub_agents_refreshed", count=n)
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("setup.router_client_refresh_failed error={}", str(e))
    except Exception:
        logger.exception("setup._update_config_and_refresh_clients.unexpected_error")


async def _restart_qq_bot_after_save() -> None:
    """QQ 凭证保存后异步重启 QQ bot 任务（不阻塞 API 返回）。

    根因修复：用户在 WebUI 填入 QQ ID/Secret 后，原代码仅更新 .env 和 os.environ，
    但 qq_bot_adapter 模块级 APP_ID/APP_SECRET 仍为空（import 时一次性读取），
    且 _start_services 不会重新调用，导致 QQ bot 永远不会启动 → QQ 显示机器人离线。

    修复：调用 web.server.restart_qq_bot_task 完成重启流程：
    1. 取消已存在的 qq_task（旧凭证实例）
    2. 更新 qq_bot_adapter.APP_ID/APP_SECRET 模块级变量
    3. 启动新的 qq_task
    """
    try:
        from web.app_ref import get_app
        from web.server import restart_qq_bot_task
        _app = get_app()
        if not _app or not hasattr(_app, "state"):
            logger.warning("setup.qq_bot_restart_skipped reason=no_app_state")
            return
        started = await restart_qq_bot_task(_app)
        logger.info("setup.qq_bot_restarted after_credential_save started={}", started)
    except (ImportError, RuntimeError, AttributeError, OSError) as e:
        logger.warning("setup.qq_bot_restart_failed error={}", str(e))
    except Exception:
        logger.exception("setup._restart_qq_bot_after_save.unexpected_error")


async def _background_reinit() -> None:
    """后台异步重初始化核心（不阻塞 API 返回）。"""
    try:
        from web.app_ref import get_app, get_start_services
        _app = get_app()
        if hasattr(_app, "state") and hasattr(_app.state, "core"):
            core = _app.state.core
            if not core._initialized:
                logger.info("setup.reinitializing_core")
                await core.init(reinit=True)
                if core._initialized:
                    _start_services = get_start_services()
                    await _start_services(_app, core)
                    logger.info("setup.core_reinitialized")
                    try:
                        registry = getattr(_app.state, "agent_registry", None)
                        if registry:
                            await registry.load_persisted()
                            logger.info("setup.registry_refreshed")
                    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
                        logger.warning("setup.registry_refresh_failed error={}", str(e))
                    except Exception:
                        logger.exception("setup._background_reinit.unexpected_error")
                else:
                    logger.error("setup.core_reinit_failed reason=still_not_initialized")
    except (OSError, RuntimeError, ValueError, ImportError, AttributeError) as e:
        import traceback
        logger.error("setup.core_reinit_failed error={} traceback={}", str(e), traceback.format_exc())
    except Exception:
        logger.exception("setup._background_reinit.unexpected_error")
    finally:
        _reinit_tasks[:] = [t for t in _reinit_tasks if not t.done()]


async def _reinit_and_maybe_restart_qq(qq_changed: bool) -> None:
    """串行执行核心重初始化与 QQ Bot 重启，消除双 Bot 竞态。

    根因：save_keys 原实现独立创建 _background_reinit() 与
    _restart_qq_bot_after_save() 两个后台任务，两者各自碰 app.state.qq_task。
    即使 Task 4 让两者走同一把锁，执行顺序仍不确定——force 重启可能先于
    core.init() 完成，导致 _start_services 创建的旧 task 与 force 重启创建的
    新 task 在取消传播窗口内同时存活。串行化后 _background_reinit 完成再
    执行 QQ 重启，且只 create_task 一次，从根本上消除竞态。
    """
    try:
        await _background_reinit()
        if qq_changed:
            await _restart_qq_bot_after_save()
    finally:
        _reinit_tasks[:] = [t for t in _reinit_tasks if not t.done()]


# 已知 Provider 映射（有 API Key 即自动注册）— 字段单一来源：provider catalog。
# 本模块只叠加展示顺序与「本地 URL 型 provider 走 *_BASE_URL」的注册策略。
_SETUP_PROVIDER_ORDER = (
    "mimo", "siliconflow", "deepseek", "openrouter",
    "modelscope", "agnes", "ollama", "llama.cpp",
)
_URL_KEYED_LOCAL_PROVIDERS = frozenset({"ollama", "llama.cpp"})


def _derive_known_providers() -> dict[str, dict[str, Any]]:
    """从 provider catalog 派生「有 Key 即自动注册」表。

    id/base_url/label/env 别名全部来自 config.get_provider_catalog()；
    env 键选择规则：优先 {ID}_API_KEY 别名（向导表单字段名契约），否则首个别名。
    catalog 加载失败（元数据 JSON 缺失，降级为空 catalog）时返回空表，不炸。
    """
    from config import get_provider_catalog
    from config_providers import get_provider_env_prefix, get_provider_label
    from llm_gateway.contracts import ProviderProtocol

    catalog = get_provider_catalog()
    derived: dict[str, dict[str, Any]] = {}
    for pid in _SETUP_PROVIDER_ORDER:
        try:
            definition = catalog.get(pid)
        except KeyError:
            continue
        aliases = definition.auth.environment_aliases
        env_prefix = get_provider_env_prefix(pid)
        if pid in _URL_KEYED_LOCAL_PROVIDERS:
            env_key = f"{env_prefix}_BASE_URL"
        else:
            env_key = next(
                (alias for alias in aliases if alias == f"{env_prefix}_API_KEY"),
                aliases[0] if aliases else "",
            )
        if not env_key:
            continue
        derived[env_key] = {
            "id": pid,
            "label": get_provider_label(pid),
            # ollama 协议本地端点同样走 OpenAI 兼容客户端（与 ProviderService._record 规则一致）
            "format": "anthropic" if definition.protocol is ProviderProtocol.ANTHROPIC else "openai",
            "base_url": get_base_url_for_provider(pid).rstrip("/"),
            "builtin": definition.builtin,
        }
    return derived


_KNOWN_PROVIDERS = _derive_known_providers()


async def _auto_register_providers(updates: dict) -> list[Any]:
    """当用户配置了免费模型平台的 Key，自动注册为自定义 Provider。

    provider_service 未就绪（应用尚未完成启动）时降级跳过，不阻塞保存流程。
    """
    from llm_gateway.provider_service import ProviderService
    from web.app_ref import get_app

    app = get_app()
    service = getattr(getattr(app, "state", None), "provider_service", None)
    if service is None:
        logger.warning("setup.auto_provider_register_skipped reason=provider_service_unavailable")
        return []
    existing = {definition.id for definition in service.list()}
    known_keys = list(_KNOWN_PROVIDERS.keys())
    provider_snapshots = []

    try:
        for env_key, provider_info in _KNOWN_PROVIDERS.items():
            if env_key in ("OLLAMA_BASE_URL", "LLAMA_CPP_BASE_URL"):
                base_url = updates.get(env_key, "").strip()
                if not base_url:
                    continue
                api_key = provider_info["id"]  # 本地部署无需真实 Key，用 provider id 占位
            else:
                api_key = updates.get(env_key, "").strip()
                if not api_key:
                    continue
                base_url = provider_info.get("base_url", "")

            pid = provider_info["id"]
            definition = service.catalog.get(pid)
            record = ProviderService._record(definition)
            record.update({
                "id": pid,
                "label": provider_info["label"],
                "base_url": base_url,
                "order": known_keys.index(env_key),
            })
            if pid in existing:
                if hasattr(service, "snapshot"):
                    provider_snapshots.append(service.snapshot(pid))
                else:
                    provider_snapshots.append((pid, ProviderService._record(definition), service.credentials.read(pid)))
                if definition.builtin:
                    await service.bind_builtin(pid, record, {"api_key": api_key})
                else:
                    await service.update(pid, record, {"api_key": api_key})
            else:
                if hasattr(service, "snapshot"):
                    provider_snapshots.append(service.snapshot(pid))
                await service.create(record, {"api_key": api_key})
                if not hasattr(service, "snapshot"):
                    provider_snapshots.append((pid, None, ""))
            existing.add(pid)
            logger.info("setup.auto_provider_registered id={} order={}", pid, known_keys.index(env_key))
    except (OSError, ValueError, RuntimeError, KeyError, AttributeError):
        await _rollback_auto_registered_providers(provider_snapshots, service)
        raise
    return provider_snapshots


async def _rollback_auto_registered_providers(
    provider_snapshots: list[Any],
    service: Any | None = None,
) -> None:
    if service is None:
        from web.app_ref import get_app
        app = get_app()
        service = getattr(getattr(app, "state", None), "provider_service", None)
    if service is None:
        logger.warning("setup.provider_rollback_skipped reason=provider_service_unavailable")
        return
    failures = []
    for snapshot in reversed(provider_snapshots):
        try:
            if hasattr(service, "restore_snapshot"):
                await service.restore_snapshot(snapshot)
            else:
                provider_id, old_record, old_credential = snapshot
                if old_record is None:
                    await service.delete(provider_id)
                else:
                    definition = service.catalog.get(provider_id)
                    if definition.builtin:
                        await service.bind_builtin(provider_id, old_record, {"api_key": old_credential})
                    else:
                        await service.update(provider_id, old_record, {"api_key": old_credential})
        except (OSError, ValueError, RuntimeError, KeyError, AttributeError) as error:
            failures.append(error)
        except Exception:
            logger.exception("setup._rollback_auto_registered_providers.unexpected_error")
    if failures:
        raise ExceptionGroup("provider snapshot rollback failed", failures)


# ── USER.md 个人资料配置 ────────────────────────────────────

import platform as _platform
import re as _re
import socket as _socket
import time as _time


def _detect_device_info_for_profile() -> dict:
    """检测设备信息用于 USER.md"""
    info = {
        "hostname": _socket.gethostname(),
        "system": _platform.system(),
        "machine": _platform.machine(),
    }
    try:
        import distro
        info["distro"] = f"{distro.name()} {distro.version()}"
    except ImportError:
        info["distro"] = _platform.platform()
    return info


def _clean_md_value(val: str) -> str:
    """清除模板占位符，返回空字符串"""
    v = val.strip()
    if v.startswith(("（", "(")):
        return ""
    if v in ("待填写", "待自动检测", "暂无"):
        return ""
    return v


def _extract_list_fields(block: str, mapping: list[tuple[str, str]]) -> dict[str, str]:
    """从 markdown 列表块中按正则模式批量提取字段。

    mapping: [(field_name, regex_pattern), ...]
    """
    result: dict[str, str] = {}
    for field_name, pattern in mapping:
        m = _re.search(pattern, block)
        if m:
            result[field_name] = _clean_md_value(m.group(1))
    return result


_USER_INFO_FIELD_MAP = [
    ("address_term", r'-\s*称呼[：:]\s*(.+)'),
    ("name", r'-\s*姓名[：:]\s*(.+)'),
    ("device", r'-\s*设备[：:]\s*(.+)'),
    ("timezone", r'-\s*时区[：:]\s*(.+)'),
]

_PERSONALITY_FIELD_MAP = [
    ("preferred_personality", r'-\s*偏好的助手人格[：:]\s*(.+)'),
    ("preferred_tone", r'-\s*偏好语气[：:]\s*(.+)'),
    ("like_to_be_called", r'-\s*喜欢被称呼为[：:]\s*(.+)'),
]

_REPLY_FIELD_MAP = [
    ("liked_reply_style", r'-\s*喜欢的回复风格[：:]\s*(.+)'),
    ("disliked_reply_style", r'-\s*不喜欢的回复风格[：:]\s*(.+)'),
]


def _parse_user_md(content: str) -> dict:
    """解析 USER.md 内容为结构化字段"""
    fields = {
        "address_term": "", "name": "", "device": "", "timezone": "",
        "preferred_personality": "", "preferred_tone": "", "like_to_be_called": "",
        "liked_reply_style": "", "disliked_reply_style": "",
        "project_preferences": "", "history_notes": "",
    }

    user_info_match = _re.search(r'## .+信息\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if user_info_match:
        fields.update(_extract_list_fields(user_info_match.group(1), _USER_INFO_FIELD_MAP))

    personality_match = _re.search(r'### 助手人格\s*\n(.*?)(?=\n### |\n## |\Z)', content, _re.DOTALL)
    if personality_match:
        fields.update(_extract_list_fields(personality_match.group(1), _PERSONALITY_FIELD_MAP))

    reply_match = _re.search(r'### 回复偏好\s*\n(.*?)(?=\n### |\n## |\Z)', content, _re.DOTALL)
    if reply_match:
        fields.update(_extract_list_fields(reply_match.group(1), _REPLY_FIELD_MAP))

    project_match = _re.search(r'### 项目偏好\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if project_match:
        block = project_match.group(1).strip()
        if not block.startswith("（"):
            fields["project_preferences"] = block

    history_match = _re.search(r'## 历史交互要点\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if history_match:
        block = history_match.group(1).strip()
        if block and not block.startswith("（暂无"):
            fields["history_notes"] = block

    return fields


_DEFAULT_PROJECT_PREFS = [
    "- 修改代码前先理解现有结构",
    "- 尽量不要大改项目，优先最小修改",
    "- 优先解决实际报错",
    "- 命令和路径要写清楚",
    "- 遇到危险操作要提醒确认",
]


def _bullet_lines(raw: str) -> list[str]:
    """将多行文本转为 markdown 列表行（确保每行以 '- ' 开头）。"""
    result: list[str] = []
    for line in raw.split("\n"):
        ln = line.strip()
        if ln:
            if not ln.startswith("-"):
                ln = f"- {ln}"
            result.append(ln)
    return result


def _build_user_md(fields: dict) -> str:
    """从结构化字段重建 USER.md 内容"""
    dev = fields.get("device", "") or "（待自动检测）"
    tz = fields.get("timezone", "") or "Asia/Shanghai"
    addr = fields.get('address_term', '') or '用户'

    lines = [
        f"# USER.md - {addr}的资料与偏好",
        "",
        "> 首次使用时自动生成，请根据需要修改以下内容。",
        "",
        f"## {addr}信息",
        "",
        f"- 称呼：{fields.get('address_term', '') or '（待填写）'}",
        f"- 姓名：{fields.get('name', '') or '（待填写）'}",
        f"- 设备：{dev}",
        f"- 时区：{tz}",
        "",
        "## 偏好设置",
        "",
        "### 助手人格",
        "",
        f"- 偏好的助手人格：{fields.get('preferred_personality', '') or '（待填写）'}",
        f"- 偏好语气：{fields.get('preferred_tone', '') or '（待填写）'}",
        f"- 喜欢被称呼为：{fields.get('like_to_be_called', '') or '（待填写）'}",
        "",
        "### 回复偏好",
        "",
        f"- 喜欢的回复风格：{fields.get('liked_reply_style', '') or '（待填写）'}",
        f"- 不喜欢的回复风格：{fields.get('disliked_reply_style', '') or '（待填写）'}",
        "",
        "### 项目偏好",
        "",
    ]

    proj_prefs = fields.get("project_preferences", "").strip()
    lines.extend(_bullet_lines(proj_prefs) if proj_prefs else _DEFAULT_PROJECT_PREFS)

    lines.extend(["", "## 历史交互要点", ""])

    history = fields.get("history_notes", "").strip()
    lines.extend(_bullet_lines(history) if history else ["- （暂无，使用过程中会自动积累）"])

    lines.append("")
    return "\n".join(lines)


def _is_template_section(name: str, addr: str = "用户") -> bool:
    """判断 USER.md 的 ``## `` 区块是否为 ``_build_user_md`` 重建的模板区块。

    模板区块：固定的 ``偏好设置`` / ``历史交互要点``，以及动态标题的
    ``## {称呼}信息``（如 ``## 爸爸信息``；默认称呼为 ``用户``，
    兼容旧格式 ``## 用户信息``）。
    其余区块（免责协议、XP 动态认知等）由系统或外部写入，必须保留。

    注意：只能精确匹配 ``{addr}信息`` 这一个标题。用 ``endswith("信息")``
    会把 ``## 账户信息`` 等非模板区块误判为模板并永久删除。
    """
    name = name.strip()
    if name in ("偏好设置", "历史交互要点"):
        return True
    return name == f"{addr}信息"


def _preserve_extra_sections(old_content: str, new_content: str, addr: str = "用户") -> str:
    """重建 USER.md 时保留非模板区块，避免丢失系统数据。

    ``_build_user_md`` 只重建模板区块（用户信息/偏好设置/历史交互要点）。
    若直接整体覆盖 USER.md，会删除 ``## 法律与声明``（免责协议状态）与
    ``## XP 动态认知`` 等系统写入的区块，导致用户重新同意协议等副作用。
    """
    if not old_content:
        return new_content
    extra_blocks = []
    for m in _re.finditer(r'^##\s+(.+?)\s*$\n(.*?)(?=^## |\Z)', old_content, _re.MULTILINE | _re.DOTALL):
        name = m.group(1)
        if not _is_template_section(name, addr):
            block = m.group(0).rstrip("\n")
            if block.strip():
                extra_blocks.append(block)
    if not extra_blocks:
        return new_content
    return new_content.rstrip("\n") + "\n\n" + "\n\n".join(extra_blocks) + "\n"


@router.get("/setup/user-profile", response_model=Envelope[dict], dependencies=_PROFILE_DEPS)
async def get_user_profile() -> Any:
    """读取 USER.md 内容并返回结构化字段"""
    from config import WORKSPACE_DIR

    user_md_path = WORKSPACE_DIR / "USER.md"
    content = ""
    if user_md_path.exists():
        try:
            content = user_md_path.read_text(encoding="utf-8-sig")
        except (OSError, PermissionError, FileNotFoundError) as exc:
            logger.debug("setup.user_md_read_failed encoding=utf-8-sig: {}", exc, exc_info=True)
            try:
                content = user_md_path.read_text(encoding="utf-8")
            except (OSError, PermissionError, FileNotFoundError) as exc2:
                logger.debug("setup.user_md_read_failed encoding=utf-8: {}", exc2, exc_info=True)
            except Exception:
                logger.exception("setup.user_md_read_failed unexpected_error")
                content = ""

        except Exception:
            logger.exception("setup.get_user_profile.unexpected_error")
    fields = _parse_user_md(content)

    # 自动检测设备信息和时区（如果未填写）
    if not fields["device"] or fields["device"] == "（待自动检测）":
        dev = _detect_device_info_for_profile()
        fields["device"] = f"{dev['hostname']}（{dev['system']} {dev['machine']}）"

    if not fields["timezone"] or fields["timezone"] == "（待自动检测）":
        fields["timezone"] = _time.tzname[0] if _time.tzname else "Asia/Shanghai"

    return Envelope(data=fields)


@router.post("/setup/user-profile", response_model=Envelope[dict], dependencies=_PROFILE_DEPS)
async def save_user_profile(body: dict) -> Any:
    """保存用户资料到 USER.md"""
    from config import WORKSPACE_DIR

    fields = {
        "address_term": body.get("address_term", "").strip(),
        "name": body.get("name", "").strip(),
        "device": body.get("device", "").strip(),
        "timezone": body.get("timezone", "").strip(),
        "preferred_personality": body.get("preferred_personality", "").strip(),
        "preferred_tone": body.get("preferred_tone", "").strip(),
        "like_to_be_called": body.get("like_to_be_called", "").strip(),
        "liked_reply_style": body.get("liked_reply_style", "").strip(),
        "disliked_reply_style": body.get("disliked_reply_style", "").strip(),
        "project_preferences": body.get("project_preferences", "").strip(),
        "history_notes": body.get("history_notes", "").strip(),
    }

    content = _build_user_md(fields)

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    user_md_path = WORKSPACE_DIR / "USER.md"
    old_content = ""
    if user_md_path.exists():
        try:
            old_content = user_md_path.read_text(encoding="utf-8-sig")
        except (OSError, PermissionError, FileNotFoundError) as exc:
            logger.debug("setup.user_md_read_for_preserve_failed: {}", exc, exc_info=True)
        except Exception:
            logger.exception("setup.save_user_profile.unexpected_error")
    content = _preserve_extra_sections(old_content, content, fields.get("address_term") or "用户")
    user_md_path.write_text(content, encoding="utf-8-sig")

    # 清除 system prompt 缓存，使修改立即生效
    try:
        import prompt_builder
        prompt_builder.clear_module_cache()
        prompt_builder._SYSTEM_PROMPT_CACHE = ""
        prompt_builder._SYSTEM_PROMPT_CACHE_TS = 0.0
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("setup.prompt_cache_clear_failed: {}", exc, exc_info=True)
    except Exception:
        logger.exception("setup.save_user_profile.unexpected_error")
    logger.info("setup.user_profile_saved path={}", str(user_md_path))
    return Envelope(data={"saved": True})


# ── 品牌署名 & 免责协议 ────────────────────────────────────


def _read_disclaimer_status(user_md_path: Path) -> dict:
    """读取 USER.md 中的免责协议状态。

    返回 ``{"agreed": bool, "agreed_at": str}``。
    文件不存在或未找到 ``## 法律与声明`` 区块时 ``agreed=False``。
    """
    result = {"agreed": False, "agreed_at": ""}
    if not user_md_path.exists():
        return result
    try:
        content = user_md_path.read_text(encoding="utf-8-sig")
    except (OSError, PermissionError, FileNotFoundError) as exc:
        logger.debug("setup.disclaimer_read_failed encoding=utf-8-sig: {}", exc, exc_info=True)
        try:
            content = user_md_path.read_text(encoding="utf-8")
        except (OSError, PermissionError, FileNotFoundError) as exc2:
            logger.debug("setup.disclaimer_read_failed encoding=utf-8: {}", exc2, exc_info=True)
        except Exception:
            logger.exception("setup.disclaimer_read_failed unexpected_error")
            return result
    except Exception:
        logger.exception("setup._read_disclaimer_status.unexpected_error")
        return result
    # 匹配 ## 法律与声明 区块（直到下一个 ## 区块或文件结尾）
    m = _re.search(r'## 法律与声明\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if not m:
        return result
    block = m.group(1)
    agreed_m = _re.search(r'disclaimer_agreed:\s*(true|false)', block, _re.IGNORECASE)
    if agreed_m and agreed_m.group(1).lower() == "true":
        result["agreed"] = True
        at_m = _re.search(r'disclaimer_agreed_at:\s*(.+)', block)
        if at_m:
            result["agreed_at"] = at_m.group(1).strip()
    return result


def _write_disclaimer_agreement(user_md_path: Path, agreed: bool) -> str:
    """写入或替换 USER.md 的 ``## 法律与声明`` 区块，返回 agreed_at 的 ISO 时间字符串。

    若已存在该区块则替换；否则追加到文件末尾。
    """

    agreed_at = _get_local_now().isoformat(timespec="seconds")
    new_section = (
        "## 法律与声明\n"
        "\n"
        f"disclaimer_agreed: {'true' if agreed else 'false'}\n"
        f"disclaimer_agreed_at: {agreed_at}\n"
        "disclaimer_version: 1\n"
    )

    content = ""
    if user_md_path.exists():
        try:
            content = user_md_path.read_text(encoding="utf-8-sig")
        except (OSError, PermissionError, FileNotFoundError) as exc:
            logger.debug("setup.disclaimer_write_read_failed encoding=utf-8-sig: {}", exc, exc_info=True)
            try:
                content = user_md_path.read_text(encoding="utf-8")
            except (OSError, PermissionError, FileNotFoundError) as exc2:
                logger.debug("setup.disclaimer_write_read_failed encoding=utf-8: {}", exc2, exc_info=True)
            except Exception:
                logger.exception("setup.disclaimer_write_read_failed unexpected_error")
                content = ""
        except Exception:
            logger.exception("setup._write_disclaimer_agreement.unexpected_error")
    # 匹配并替换已有的 ## 法律与声明 区块（直到下一个 ## 区块或文件结尾）
    pattern = _re.compile(r'## 法律与声明\s*\n.*?(?=\n## |\Z)', _re.DOTALL)
    if pattern.search(content):
        new_content = pattern.sub(lambda _m: new_section, content)
    else:
        # 追加到文件末尾
        if content and not content.endswith("\n"):
            content += "\n"
        new_content = content + ("\n" if content else "") + new_section

    user_md_path.parent.mkdir(parents=True, exist_ok=True)
    user_md_path.write_text(new_content, encoding="utf-8-sig")
    return agreed_at


@router.get("/brand/signature", response_model=Envelope[dict])
async def get_brand_signature() -> Any:
    """返回品牌署名信息（无需认证）。

    供前端定期校验署名是否被篡改。版本号复用 ``GET /setup/version`` 逻辑。
    """
    from web.routers.system import _read_version
    return Envelope(data={
        "signature": "本 Agent 由小妲的老父亲-飞 个人学习用途二创开发",
        "author": "小妲的老父亲-飞",
        "version": _read_version(),
    })


@router.get("/setup/disclaimer-status", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def get_disclaimer_status() -> Any:
    """返回免责协议状态（是否已同意、同意时间）与协议全文。

    首次运行免认证，非首次需认证。USER.md 不存在时 ``agreed=false``。
    """
    from config import WORKSPACE_DIR

    user_md_path = WORKSPACE_DIR / "USER.md"
    try:
        status = _read_disclaimer_status(user_md_path)
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("setup.disclaimer_status.read_failed error={}", str(e))
        status = {"agreed": False, "agreed_at": ""}
    except Exception:
        logger.exception("setup.get_disclaimer_status.unexpected_error")
    return Envelope(data={
        "agreed": status["agreed"],
        "agreed_at": status["agreed_at"],
        "text": DISCLAIMER_TEXT,
    })


@router.post("/setup/agree-disclaimer", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def agree_disclaimer(body: dict) -> Any:
    """记录用户对免责协议的同意状态到 USER.md。

    首次运行免认证，非首次需认证。接收 JSON body ``{"agreed": true}``，
    写入失败返回 500。
    """
    from config import WORKSPACE_DIR

    agreed = bool(body.get("agreed", False))
    user_md_path = WORKSPACE_DIR / "USER.md"
    try:
        agreed_at = _write_disclaimer_agreement(user_md_path, agreed)
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.error("setup.agree_disclaimer.write_failed error={}", str(e))
        raise HTTPException(status_code=500, detail=f"写入免责协议失败: {e}") from None
    except Exception as e:
        logger.exception("setup.agree_disclaimer.unexpected_error")
        raise HTTPException(status_code=500, detail=f"写入免责协议失败: {e}") from None
    logger.info("setup.disclaimer_agreed path={} agreed={}", str(user_md_path), agreed)
    return Envelope(data={
        "success": True,
        "agreed": agreed,
        "agreed_at": agreed_at,
    })
