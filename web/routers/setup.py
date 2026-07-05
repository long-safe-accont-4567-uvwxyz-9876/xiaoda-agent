from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.routers.auth import get_current_user
from web.schemas import Envelope


# 免责协议全文（模块级常量，供前端通过 API 获取）
DISCLAIMER_TEXT = """本 Agent 由纳西妲的老父亲-"飞"个人学习用途二创开发，禁止用户生成任何违禁内容，禁止用于任何商业用途，否则一切后果与开发者无关，由用户一人承担。

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


async def _is_first_run_or_authenticated(request: Request) -> str:
    """认证依赖：首次运行（.env 不存在或 MIMO_API_KEY 为空）时允许无认证访问；
    非首次运行时必须携带有效 Bearer Token。返回用户标识。"""
    try:
        from setup_wizard import is_first_run
        first_run = is_first_run()
    except Exception as e:
        # 降级：无法判断时允许访问，避免把首次安装流程锁死
        logger.warning("setup.first_run_check_failed error={} -> allow", str(e))
        first_run = True
    if first_run:
        return "setup"
    return await get_current_user(request)


router = APIRouter(tags=["setup"])

# 需要认证的端点共享的依赖列表（首次运行时免认证）
_AUTH_DEPS = [Depends(_is_first_run_or_authenticated)]


@router.get("/setup/first-run", response_model=Envelope[dict])
async def get_first_run() -> Any:
    """检测是否首次运行（.env 不存在或 MIMO_API_KEY 为空），
    以及用户资料是否已配置。"""
    # 1. 检测 API Key 是否已配置
    first_run = True
    try:
        from setup_wizard import is_first_run
        first_run = is_first_run()
    except Exception as e:
        logger.error("setup.first_run_import_failed error={}", str(e))
        import sys
        if getattr(sys, 'frozen', False):
            env_dir = os.path.dirname(sys.executable)
        else:
            env_dir = os.path.dirname(os.path.abspath(__file__))
            for _ in range(3):
                env_dir = os.path.dirname(env_dir)
        env_path = os.path.join(env_dir, ".env")
        if not os.path.exists(env_path):
            first_run = True
        else:
            try:
                with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("MIMO_API_KEY="):
                            val = line.strip().split("=", 1)[1].strip().strip("'\"")
                            if val:
                                first_run = False
                                break
            except Exception as exc:
                logger.debug("setup.env_read_failed: {}", exc, exc_info=True)

    # 2. 检测用户资料是否已配置（USER.md 存在且有实际填写的称呼和姓名）
    profile_done = False
    try:
        from config import WORKSPACE_DIR
        user_md = WORKSPACE_DIR / "USER.md"
        if user_md.exists():
            content = user_md.read_text(encoding="utf-8-sig")
            # 检查称呼和姓名字段是否已填写（不是占位符）
            import re as _re
            addr = _re.search(r'-\s*称呼[：:]\s*(.+)', content)
            name = _re.search(r'-\s*姓名[：:]\s*(.+)', content)
            if addr and name:
                addr_val = addr.group(1).strip()
                name_val = name.group(1).strip()
                if addr_val and not addr_val.startswith("（") and name_val and not name_val.startswith("（"):
                    profile_done = True
    except Exception as exc:
        logger.debug("setup.profile_check_failed: {}", exc, exc_info=True)

    return Envelope(data={"first_run": first_run, "profile_done": profile_done})


@router.get("/setup/version", response_model=Envelope[dict])
async def get_version() -> Any:
    """获取安装包版本号（无需认证）"""
    from web.routers.system import _read_version
    return Envelope(data={"version": _read_version()})


@router.get("/setup/keys", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def get_keys() -> Any:
    """返回所有 Key 的配置状态（脱敏）。"""
    import sys
    logger.info("setup.keys.called frozen={} exe={}", getattr(sys, 'frozen', False), getattr(sys, 'executable', 'N/A'))
    try:
        from setup_wizard import REQUIRED_KEYS, OPTIONAL_KEYS, _load_env_values
        logger.info("setup.keys.import_ok")
    except Exception as e:
        logger.error("setup.keys.import_failed error={}", str(e))
        # 降级：返回硬编码的 key 列表
        REQUIRED_KEYS = [
            {"key": "MIMO_API_KEY", "label": "MiMo API 密钥", "desc": "小米 MiMo 大模型 API 密钥", "url": "https://platform.xiaomimimo.com?ref=SU5WDZ", "url_desc": "注册 → 控制台 → API Keys"},
            {"key": "QQBOT_APP_ID", "label": "QQ Bot App ID", "desc": "QQ 机器人应用 ID", "url": "https://q.qq.com", "url_desc": "创建机器人应用 → 获取 AppID"},
            {"key": "QQBOT_APP_SECRET", "label": "QQ Bot App Secret", "desc": "QQ 机器人应用密钥", "url": "https://q.qq.com", "url_desc": "同一页面的 AppSecret"},
            {"key": "EMBED_API_KEY", "label": "向量嵌入 API 密钥", "desc": "硅基流动嵌入模型密钥", "url": "https://cloud.siliconflow.cn/i/iM5RmeWc", "url_desc": "注册 → API Keys → 复制"},
        ]
        OPTIONAL_KEYS = [
            {"key": "WEBUI_PASSWORD", "label": "Web UI 密码", "desc": "留空则无需密码登录", "url": "", "url_desc": ""},
            {"key": "TAVILY_API_KEY", "label": "Tavily 搜索 API 密钥", "desc": "AI 搜索引擎", "url": "https://tavily.com", "url_desc": "注册 → API Keys"},
            {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow API 密钥", "desc": "硅基流动 API 密钥", "url": "https://cloud.siliconflow.cn/i/iM5RmeWc", "url_desc": "注册 → API Keys"},
            {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API 密钥", "desc": "DeepSeek 大模型 API 密钥", "url": "https://platform.deepseek.com", "url_desc": "注册 → API Keys"},
            {"key": "OPENROUTER_API_KEY", "label": "OpenRouter API 密钥", "desc": "OpenRouter API 密钥", "url": "https://openrouter.ai", "url_desc": "注册 → API Keys"},
            {"key": "WOLFRAMALPHA_API_KEY", "label": "WolframAlpha 知识计算密钥", "desc": "知识计算引擎", "url": "https://products.wolframalpha.com/api/", "url_desc": "注册 → Get AppID"},
            {"key": "AGNES_API_KEY", "label": "Agnes AI 图像/视频密钥", "desc": "图片生成和视频生成的核心依赖", "url": "https://agnes-ai.com", "url_desc": "注册 → API Keys"},
            {"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "GitHub 个人访问令牌", "desc": "GitHub MCP Server 所需", "url": "https://github.com/settings/tokens", "url_desc": "Generate new token"},
            {"key": "MODELSCOPE_ACCESS_TOKEN", "label": "魔搭 Access Token", "desc": "魔搭 ModelScope 免费模型发现", "url": "https://modelscope.cn", "url_desc": "注册 → 个人中心 → 访问令牌"},
        ]
        _load_env_values = lambda: {}

    try:
        current = _load_env_values()
    except Exception as e:
        logger.error("setup.keys.load_env_failed error={}", str(e))
        current = {}

    keys: list[dict[str, Any]] = []

    for item in REQUIRED_KEYS:
        key = item["key"]
        val = current.get(key, "")
        keys.append({
            "key": key,
            "label": item["label"],
            "desc": item["desc"],
            "url": item.get("url", ""),
            "url_desc": item.get("url_desc", ""),
            "required": True,
            "configured": bool(val.strip()),
            "masked_value": _mask_key_value(val),
        })

    for item in OPTIONAL_KEYS:
        key = item["key"]
        val = current.get(key, "")
        keys.append({
            "key": key,
            "label": item["label"],
            "desc": item["desc"],
            "url": item.get("url", ""),
            "url_desc": item.get("url_desc", ""),
            "required": False,
            "configured": bool(val.strip()),
            "masked_value": _mask_key_value(val),
        })

    return Envelope(data={"keys": keys})


_TIMEOUT = 10.0


async def _test_mimo(key_value: str) -> tuple[bool, str]:
    """测试 MiMo API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.xiaomimimo.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key_value}"},
                json={
                    "model": "mimo-v2.5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("choices"):
                    return True, "MiMo API Key 验证成功"
                return False, "MiMo 返回了异常响应（无 choices）"
            return False, f"MiMo API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "MiMo API 请求超时"
    except Exception as e:
        return False, f"MiMo API 请求失败: {e}"


async def _test_qqbot(app_id: str, app_secret: str) -> tuple[bool, str]:
    """测试 QQ Bot App ID + App Secret。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={
                    "appId": app_id,
                    "clientSecret": app_secret,
                    "grant_type": "client_credentials",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("access_token"):
                    return True, "QQ Bot 凭证验证成功"
                return False, f"QQ Bot 返回了异常响应: {data.get('message', '无 access_token')}"
            return False, f"QQ Bot API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "QQ Bot API 请求超时"
    except Exception as e:
        return False, f"QQ Bot API 请求失败: {e}"


async def _test_siliconflow_embed(key_value: str) -> tuple[bool, str]:
    """测试 SiliconFlow 嵌入 API Key（EMBED_API_KEY）。与 _test_siliconflow 共用实现。"""
    return await _test_siliconflow(key_value)


async def _test_siliconflow(key_value: str) -> tuple[bool, str]:
    """测试 SiliconFlow API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.siliconflow.cn/v1/embeddings",
                headers={"Authorization": f"Bearer {key_value}"},
                json={"model": "BAAI/bge-large-zh-v1.5", "input": "test"},
            )
            if resp.status_code == 200:
                return True, "SiliconFlow API Key 验证成功"
            return False, f"SiliconFlow API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "SiliconFlow API 请求超时"
    except Exception as e:
        return False, f"SiliconFlow API 请求失败: {e}"


async def _test_openrouter(key_value: str) -> tuple[bool, str]:
    """测试 OpenRouter API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key_value}"},
            )
            if resp.status_code == 200:
                return True, "OpenRouter API Key 验证成功"
            return False, f"OpenRouter API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "OpenRouter API 请求超时"
    except Exception as e:
        return False, f"OpenRouter API 请求失败: {e}"


async def _test_deepseek(key_value: str) -> tuple[bool, str]:
    """测试 DeepSeek API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.deepseek.com/v1/models",
                headers={"Authorization": f"Bearer {key_value}"},
            )
            if resp.status_code == 200:
                return True, "DeepSeek API Key 验证成功"
            return False, f"DeepSeek API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "DeepSeek API 请求超时"
    except Exception as e:
        return False, f"DeepSeek API 请求失败: {e}"


async def _test_agnes(key_value: str) -> tuple[bool, str]:
    """测试 Agnes AI API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://apihub.agnes-ai.com/v1/models",
                headers={"Authorization": f"Bearer {key_value}"},
            )
            if resp.status_code == 200:
                return True, "Agnes AI API Key 验证成功"
            return False, f"Agnes AI API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "Agnes AI API 请求超时"
    except Exception as e:
        return False, f"Agnes AI API 请求失败: {e}"


async def _test_wolframalpha(key_value: str) -> tuple[bool, str]:
    """测试 WolframAlpha API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.wolframalpha.com/v2/query",
                params={
                    "appid": key_value,
                    "input": "test",
                    "format": "plaintext",
                    "output": "json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                query_result = data.get("queryresult", {})
                if query_result.get("success") is True or query_result.get("error") is False:
                    return True, "WolframAlpha API Key 验证成功"
                # 即使查询本身失败（如 input 不明确），只要 key 有效就会返回 200
                # 检查是否有 error 字段表明 key 无效
                error_obj = query_result.get("error")
                if isinstance(error_obj, dict) and error_obj.get("code") == 1:
                    return False, "WolframAlpha API Key 无效"
                return True, "WolframAlpha API Key 验证成功"
            return False, f"WolframAlpha API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "WolframAlpha API 请求超时"
    except Exception as e:
        return False, f"WolframAlpha API 请求失败: {e}"


async def _test_modelscope(key_value: str) -> tuple[bool, str]:
    """测试 ModelScope Access Token。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://modelscope.cn/api/v1/models",
                headers={"Authorization": f"Bearer {key_value}"},
            )
            if resp.status_code == 200:
                return True, "ModelScope Access Token 验证成功"
            return False, f"ModelScope API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "ModelScope API 请求超时"
    except Exception as e:
        return False, f"ModelScope API 请求失败: {e}"


async def _test_tavily(key_value: str) -> tuple[bool, str]:
    """测试 Tavily API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key_value,
                    "query": "test",
                    "max_results": 1,
                },
            )
            if resp.status_code == 200:
                return True, "Tavily API Key 验证成功"
            return False, f"Tavily API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "Tavily API 请求超时"
    except Exception as e:
        return False, f"Tavily API 请求失败: {e}"


async def _test_github(key_value: str) -> tuple[bool, str]:
    """测试 GitHub Personal Access Token。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {key_value}",
                    "Accept": "application/vnd.github.v3+json",
                },
            )
            if resp.status_code == 200:
                return True, "GitHub Personal Access Token 验证成功"
            if resp.status_code == 401:
                return False, "GitHub Token 无效或已过期"
            return False, f"GitHub API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "GitHub API 请求超时"
    except Exception as e:
        return False, f"GitHub API 请求失败: {e}"


async def _test_ollama(base_url: str) -> tuple[bool, str]:
    """测试 Ollama 服务连通性。"""
    # SSRF 防护：校验 URL 不指向内网/元数据服务
    from security.ssrf_guard import validate_url
    allowed, reason = validate_url(base_url)
    if not allowed:
        return False, f"URL 安全检查失败: {reason}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models")
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", [])
                return True, f"Ollama 可用，发现 {len(models)} 个模型"
            return False, f"Ollama 返回 HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, f"无法连接到 Ollama 服务（{base_url}），请确认 Ollama 已启动"
    except httpx.TimeoutException:
        return False, "Ollama 连接超时"
    except Exception as e:
        return False, f"Ollama 请求失败: {e}"


async def test_single_key(key_name: str, key_value: str, extra: dict | None = None) -> tuple[bool, str]:
    """根据 key_name 调用对应的测试函数，返回 (success, message)。"""
    extra = extra or {}

    if key_name == "MIMO_API_KEY":
        return await _test_mimo(key_value)

    if key_name == "QQBOT_APP_ID":
        app_secret = extra.get("QQBOT_APP_SECRET", "")
        if not app_secret:
            return False, "QQ Bot 需要同时提供 APP_ID 和 APP_SECRET"
        return await _test_qqbot(key_value, app_secret)

    if key_name == "QQBOT_APP_SECRET":
        app_id = extra.get("QQBOT_APP_ID", "")
        if not app_id:
            return False, "QQ Bot 需要同时提供 APP_ID 和 APP_SECRET"
        return await _test_qqbot(app_id, key_value)

    if key_name == "EMBED_API_KEY":
        return await _test_siliconflow_embed(key_value)

    if key_name == "SILICONFLOW_API_KEY":
        return await _test_siliconflow(key_value)

    if key_name == "DEEPSEEK_API_KEY":
        return await _test_deepseek(key_value)

    if key_name == "OPENROUTER_API_KEY":
        return await _test_openrouter(key_value)

    if key_name == "AGNES_API_KEY":
        return await _test_agnes(key_value)

    if key_name == "WOLFRAMALPHA_API_KEY":
        return await _test_wolframalpha(key_value)

    if key_name == "MODELSCOPE_ACCESS_TOKEN":
        return await _test_modelscope(key_value)

    if key_name == "TAVILY_API_KEY":
        return await _test_tavily(key_value)

    if key_name == "GITHUB_PERSONAL_ACCESS_TOKEN":
        return await _test_github(key_value)

    if key_name == "OLLAMA_BASE_URL":
        return await _test_ollama(key_value)

    # 不需要调用外部 API 的配置项，简单校验即可
    _NO_API_TEST_KEYS = {"WEBUI_PASSWORD"}
    if key_name in _NO_API_TEST_KEYS:
        return True, "配置已保存"

    return False, "未知的 API Key 类型"


@router.post("/setup/test-key", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def test_key(body: dict) -> Any:
    """测试 API Key 是否有效。"""
    key_name = body.get("key_name", "")
    key_value = body.get("key_value", "")

    if not key_name or not key_value:
        return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 key_name 和 key_value"})

    extra = body.get("extra", {})
    success, message = await test_single_key(key_name, key_value, extra)

    return Envelope(data={"success": success, "message": message})


@router.post("/setup/keys", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def save_keys(body: dict) -> Any:
    """将提供的 Key-Value 写入 .env 文件。"""
    from setup_wizard import (
        ENV_PATH, ENV_EXAMPLE_PATH, REQUIRED_KEYS,
        _parse_env_lines, _write_env, _load_env_values,
    )

    updates = body.get("keys")
    if not updates or not isinstance(updates, dict):
        return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 keys 字段（dict）"})

    # 当 test_required=true 时，对必填 Key 逐一测试
    test_required = body.get("test_required", False)
    if test_required:
        test_error = await _test_required_keys(updates, REQUIRED_KEYS)
        if test_error is not None:
            return test_error

    # 写入 .env 文件
    _write_env_file(updates, ENV_PATH, ENV_EXAMPLE_PATH, _parse_env_lines, _load_env_values, _write_env)
    _auto_register_providers(updates)
    logger.info("setup.keys_saved count={}", len(updates))

    # 重新加载环境变量 + 清除缓存 + 重置凭证池
    _reload_env_and_cache(updates, ENV_PATH)
    _reset_credential_pool(updates)

    # 更新 config 模块变量 + 刷新客户端
    _update_config_and_refresh_clients(updates)

    # 核心重初始化放到后台异步执行，不阻塞 API 返回
    import asyncio
    asyncio.create_task(_background_reinit())

    return Envelope(data={"saved": list(updates.keys()), "need_restart": False})


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
            logger.info("setup.copied_env_example")
        else:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write("")
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


def _reload_env_and_cache(updates: Any, ENV_PATH: Any) -> None:
    """重新加载环境变量、清除模型发现缓存。"""
    import os
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    # 兜底：直接写入 os.environ
    for k, v in updates.items():
        v = v.strip() if isinstance(v, str) else ""
        if v:
            os.environ[k] = v
    # 清除模型发现缓存
    try:
        from web.routers.model_discovery import invalidate_discovery_cache
        invalidate_discovery_cache()
        logger.info("setup.discovery_cache_invalidated")
    except Exception as e:
        logger.warning("setup.discovery_cache_invalidate_failed error={}", str(e))


def _reset_credential_pool(updates: Any) -> None:
    """重置凭证池中所有 DEAD 凭证，并替换为新 Key。"""
    try:
        from utils.credential_pool import get_credential_pool, Credential
        pool = get_credential_pool()
        _PROVIDER_KEY_MAP = {
            "SILICONFLOW_API_KEY": ("siliconflow", "https://api.siliconflow.cn/v1"),
            "OPENROUTER_API_KEY": ("openrouter", "https://openrouter.ai/api/v1"),
            "MODELSCOPE_ACCESS_TOKEN": ("modelscope", "https://api-inference.modelscope.cn/v1"),
            "MIMO_API_KEY": ("mimo", "https://api.xiaomimimo.com/v1"),
            "DEEPSEEK_API_KEY": ("deepseek", "https://api.deepseek.com/v1"),
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
        logger.info("setup.credential_pool_updated")
    except Exception as e:
        logger.warning("setup.credential_pool_reset_failed error={}", str(e))


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
        from web._app_ref import get_app
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
    except Exception as e:
        logger.warning("setup.router_client_refresh_failed error={}", str(e))


async def _background_reinit() -> None:
    """后台异步重初始化核心（不阻塞 API 返回）。"""
    try:
        from web._app_ref import get_app, get_start_services
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
                    except Exception as e:
                        logger.warning("setup.registry_refresh_failed error={}", str(e))
                else:
                    logger.error("setup.core_reinit_failed reason=still_not_initialized")
    except Exception as e:
        import traceback
        logger.error("setup.core_reinit_failed error={} traceback={}", str(e), traceback.format_exc())


# 已知 Provider 映射 — 有 API Key 即自动注册
_KNOWN_PROVIDERS = {
    "MIMO_API_KEY": {
        "id": "mimo", "label": "小米 MiMo", "format": "openai",
        "base_url": "https://api.xiaomimimo.com/v1", "builtin": True,
    },
    "SILICONFLOW_API_KEY": {
        "id": "siliconflow", "label": "SiliconFlow 硅基流动", "format": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
    },
    "DEEPSEEK_API_KEY": {
        "id": "deepseek", "label": "DeepSeek", "format": "openai",
        "base_url": "https://api.deepseek.com/v1",
    },
    "OPENROUTER_API_KEY": {
        "id": "openrouter", "label": "OpenRouter", "format": "openai",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "MODELSCOPE_API_KEY": {
        "id": "modelscope", "label": "ModelScope 魔搭", "format": "openai",
        "base_url": "https://api-inference.modelscope.cn/v1",
    },
    "AGNES_API_KEY": {
        "id": "agnes", "label": "Agnes AI", "format": "openai",
        "base_url": "https://apihub.agnes-ai.com/v1",
    },
    "OLLAMA_BASE_URL": {
        "id": "ollama", "label": "Ollama 本地大模型", "format": "openai",
        "base_url": "http://localhost:11434/v1",
    },
}


def _auto_register_providers(updates: dict) -> None:
    """当用户配置了免费模型平台的 Key，自动注册为自定义 Provider。"""
    import os
    from web.config_service import get_config_service
    from web.custom_providers import register_into_router

    cfg = get_config_service()
    existing = cfg.get("models.providers", {}) or {}
    # 基于 _KNOWN_PROVIDERS 插入顺序计算 order 索引
    known_keys = list(_KNOWN_PROVIDERS.keys())

    for env_key, provider_info in _KNOWN_PROVIDERS.items():
        # Ollama 特殊处理：无需 API Key，只需要 base_url
        if env_key == "OLLAMA_BASE_URL":
            base_url = updates.get(env_key, "").strip()
            if not base_url:
                continue
            api_key = "ollama"  # 占位 Key
        else:
            api_key = updates.get(env_key, "").strip()
            if not api_key:
                continue
            base_url = provider_info.get("base_url", "")

        pid = provider_info["id"]

        # 写入凭证文件
        from web.routers.models import _key_file
        from config import get_credentials_dir
        cred_dir = get_credentials_dir()
        cred_dir.mkdir(parents=True, exist_ok=True)
        fp = cred_dir / f"provider_{pid}.key"
        fp.write_text(api_key, encoding="utf-8")
        try:
            os.chmod(fp, 0o600)
        except OSError:
            pass

        # 注册到配置（如果尚未存在）
        if pid not in existing:
            record = {
                "label": provider_info["label"],
                "format": provider_info["format"],
                "base_url": base_url,
                "default_model": "",
                "enabled": True,
                "order": known_keys.index(env_key),
            }
            if provider_info.get("builtin"):
                record["builtin"] = True
            cfg.set(f"models.providers.{pid}", record)
            logger.info("setup.auto_provider_registered id={} order={}", pid, known_keys.index(env_key))

        # 注册到运行时 router
        try:
            from model_router import ModelRouter
            # 尝试获取 router 实例 (原 import web.server as srv 已移除, 避免循环导入)
        except Exception as exc:
            logger.debug("setup.model_router_import_failed: {}", exc, exc_info=True)

        # 通过 app.state 注册（如果 app 已启动）
        try:
            from web._app_ref import get_app
            app = get_app()
            if hasattr(app, "state") and hasattr(app.state, "core"):
                router_obj = app.state.core.router
                register_into_router(
                    router_obj, pid,
                    provider_info["format"],
                    base_url,
                    api_key,
                )
                logger.info("setup.auto_provider_runtime id={}", pid)
        except Exception as e:
            logger.debug("setup.auto_provider_runtime_skip error={}", str(e))


# ── USER.md 个人资料配置 ────────────────────────────────────

import re as _re
import time as _time
import platform as _platform
import socket as _socket


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


def _parse_user_md(content: str) -> dict:
    """解析 USER.md 内容为结构化字段"""
    fields = {
        "address_term": "",
        "name": "",
        "device": "",
        "timezone": "",
        "preferred_personality": "",
        "preferred_tone": "",
        "like_to_be_called": "",
        "liked_reply_style": "",
        "disliked_reply_style": "",
        "project_preferences": "",
        "history_notes": "",
    }

    def _clean(val: str) -> str:
        """清除模板占位符，返回空字符串"""
        v = val.strip()
        if v.startswith("（") or v.startswith("("):
            return ""
        if v in ("待填写", "待自动检测", "暂无"):
            return ""
        return v

    # 解析 "## {称呼}信息" 区块（兼容动态标题）
    user_info_match = _re.search(r'## .+信息\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if user_info_match:
        block = user_info_match.group(1)
        m = _re.search(r'-\s*称呼[：:]\s*(.+)', block)
        if m: fields["address_term"] = _clean(m.group(1))
        m = _re.search(r'-\s*姓名[：:]\s*(.+)', block)
        if m: fields["name"] = _clean(m.group(1))
        m = _re.search(r'-\s*设备[：:]\s*(.+)', block)
        if m: fields["device"] = _clean(m.group(1))
        m = _re.search(r'-\s*时区[：:]\s*(.+)', block)
        if m: fields["timezone"] = _clean(m.group(1))

    # 解析 "### 助手人格" 区块
    personality_match = _re.search(r'### 助手人格\s*\n(.*?)(?=\n### |\n## |\Z)', content, _re.DOTALL)
    if personality_match:
        block = personality_match.group(1)
        m = _re.search(r'-\s*偏好的助手人格[：:]\s*(.+)', block)
        if m: fields["preferred_personality"] = _clean(m.group(1))
        m = _re.search(r'-\s*偏好语气[：:]\s*(.+)', block)
        if m: fields["preferred_tone"] = _clean(m.group(1))
        m = _re.search(r'-\s*喜欢被称呼为[：:]\s*(.+)', block)
        if m: fields["like_to_be_called"] = _clean(m.group(1))

    # 解析 "### 回复偏好" 区块
    reply_match = _re.search(r'### 回复偏好\s*\n(.*?)(?=\n### |\n## |\Z)', content, _re.DOTALL)
    if reply_match:
        block = reply_match.group(1)
        m = _re.search(r'-\s*喜欢的回复风格[：:]\s*(.+)', block)
        if m: fields["liked_reply_style"] = _clean(m.group(1))
        m = _re.search(r'-\s*不喜欢的回复风格[：:]\s*(.+)', block)
        if m: fields["disliked_reply_style"] = _clean(m.group(1))

    # 解析 "### 项目偏好" 区块
    project_match = _re.search(r'### 项目偏好\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if project_match:
        block = project_match.group(1).strip()
        # 过滤掉纯占位内容
        if not block.startswith("（"):
            fields["project_preferences"] = block

    # 解析 "## 历史交互要点" 区块
    history_match = _re.search(r'## 历史交互要点\s*\n(.*?)(?=\n## |\Z)', content, _re.DOTALL)
    if history_match:
        block = history_match.group(1).strip()
        # 去除 "（暂无..." 等占位文字
        if block and not block.startswith("（暂无"):
            fields["history_notes"] = block

    return fields


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
    if proj_prefs:
        for line in proj_prefs.split("\n"):
            line = line.strip()
            if line:
                if not line.startswith("-"):
                    line = f"- {line}"
                lines.append(line)
    else:
        lines.extend([
            "- 修改代码前先理解现有结构",
            "- 尽量不要大改项目，优先最小修改",
            "- 优先解决实际报错",
            "- 命令和路径要写清楚",
            "- 遇到危险操作要提醒确认",
        ])

    lines.extend([
        "",
        "## 历史交互要点",
        "",
    ])

    history = fields.get("history_notes", "").strip()
    if history:
        for line in history.split("\n"):
            line = line.strip()
            if line:
                if not line.startswith("-"):
                    line = f"- {line}"
                lines.append(line)
    else:
        lines.append("- （暂无，使用过程中会自动积累）")

    lines.append("")
    return "\n".join(lines)


@router.get("/setup/user-profile", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
async def get_user_profile() -> Any:
    """读取 USER.md 内容并返回结构化字段"""
    from config import WORKSPACE_DIR

    user_md_path = WORKSPACE_DIR / "USER.md"
    content = ""
    if user_md_path.exists():
        try:
            content = user_md_path.read_text(encoding="utf-8-sig")
        except Exception as exc:
            logger.debug("setup.user_md_read_failed encoding=utf-8-sig: {}", exc, exc_info=True)
            try:
                content = user_md_path.read_text(encoding="utf-8")
            except Exception as exc2:
                logger.debug("setup.user_md_read_failed encoding=utf-8: {}", exc2, exc_info=True)
                content = ""

    fields = _parse_user_md(content)

    # 自动检测设备信息和时区（如果未填写）
    if not fields["device"] or fields["device"] == "（待自动检测）":
        dev = _detect_device_info_for_profile()
        fields["device"] = f"{dev['hostname']}（{dev['system']} {dev['machine']}）"

    if not fields["timezone"] or fields["timezone"] == "（待自动检测）":
        fields["timezone"] = _time.tzname[0] if _time.tzname else "Asia/Shanghai"

    return Envelope(data=fields)


@router.post("/setup/user-profile", response_model=Envelope[dict], dependencies=_AUTH_DEPS)
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
    user_md_path.write_text(content, encoding="utf-8-sig")

    # 清除 system prompt 缓存，使修改立即生效
    try:
        import prompt_builder
        prompt_builder._SYSTEM_PROMPT_CACHE = ""
        prompt_builder._SYSTEM_PROMPT_CACHE_TS = 0.0
    except Exception as exc:
        logger.debug("setup.prompt_cache_clear_failed: {}", exc, exc_info=True)

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
    except Exception as exc:
        logger.debug("setup.disclaimer_read_failed encoding=utf-8-sig: {}", exc, exc_info=True)
        try:
            content = user_md_path.read_text(encoding="utf-8")
        except Exception as exc2:
            logger.debug("setup.disclaimer_read_failed encoding=utf-8: {}", exc2, exc_info=True)
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
    from datetime import datetime

    agreed_at = datetime.now().isoformat(timespec="seconds")
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
        except Exception as exc:
            logger.debug("setup.disclaimer_write_read_failed encoding=utf-8-sig: {}", exc, exc_info=True)
            try:
                content = user_md_path.read_text(encoding="utf-8")
            except Exception as exc2:
                logger.debug("setup.disclaimer_write_read_failed encoding=utf-8: {}", exc2, exc_info=True)
                content = ""

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
        "signature": "本 Agent 由纳西妲的老父亲-飞 个人学习用途二创开发",
        "author": "纳西妲的老父亲-飞",
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
    except Exception as e:
        logger.warning("setup.disclaimer_status.read_failed error={}", str(e))
        status = {"agreed": False, "agreed_at": ""}

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
    except Exception as e:
        logger.error("setup.agree_disclaimer.write_failed error={}", str(e))
        raise HTTPException(status_code=500, detail=f"写入免责协议失败: {e}")

    logger.info("setup.disclaimer_agreed path={} agreed={}", str(user_md_path), agreed)
    return Envelope(data={
        "success": True,
        "agreed": agreed,
        "agreed_at": agreed_at,
    })
