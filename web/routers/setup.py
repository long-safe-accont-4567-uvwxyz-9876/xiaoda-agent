from __future__ import annotations

import os
import shutil
from typing import Any

import httpx
from fastapi import APIRouter
from loguru import logger

from web.schemas import Envelope

router = APIRouter(tags=["setup"])


@router.get("/setup/first-run", response_model=Envelope[dict])
async def get_first_run():
    """检测是否首次运行（.env 不存在或 MIMO_API_KEY 为空）。"""
    try:
        from setup_wizard import is_first_run
        return Envelope(data={"first_run": is_first_run()})
    except Exception as e:
        logger.error("setup.first_run_import_failed error={}", str(e))
        # 降级：直接检查 .env 文件
        import sys
        if getattr(sys, 'frozen', False):
            env_dir = os.path.dirname(sys.executable)
        else:
            env_dir = os.path.dirname(os.path.abspath(__file__))
            # web/routers/setup.py -> 向上3级到项目根
            for _ in range(3):
                env_dir = os.path.dirname(env_dir)
        env_path = os.path.join(env_dir, ".env")
        if not os.path.exists(env_path):
            return Envelope(data={"first_run": True})
        # 简单检查 MIMO_API_KEY
        try:
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip().startswith("MIMO_API_KEY="):
                        val = line.strip().split("=", 1)[1].strip().strip("'\"")
                        if val:
                            return Envelope(data={"first_run": False})
        except Exception:
            pass
        return Envelope(data={"first_run": True})


@router.get("/setup/keys", response_model=Envelope[dict])
async def get_keys():
    """返回所有 Key 的配置状态（脱敏）。"""
    import sys
    logger.info("setup.keys.called frozen={} exe={}", getattr(sys, 'frozen', False), getattr(sys, 'executable', 'N/A'))
    try:
        from setup_wizard import REQUIRED_KEYS, OPTIONAL_KEYS, _load_env_values, _mask_value
        logger.info("setup.keys.import_ok")
    except Exception as e:
        logger.error("setup.keys.import_failed error={}", str(e))
        # 降级：返回硬编码的 key 列表
        REQUIRED_KEYS = [
            {"key": "MIMO_API_KEY", "label": "MiMo API 密钥", "desc": "小米 MiMo 大模型 API 密钥", "url": "https://xiaomimimo.com", "url_desc": "注册 → 控制台 → API Keys"},
            {"key": "QQBOT_APP_ID", "label": "QQ Bot App ID", "desc": "QQ 机器人应用 ID", "url": "https://q.qq.com", "url_desc": "创建机器人应用 → 获取 AppID"},
            {"key": "QQBOT_APP_SECRET", "label": "QQ Bot App Secret", "desc": "QQ 机器人应用密钥", "url": "https://q.qq.com", "url_desc": "同一页面的 AppSecret"},
            {"key": "EMBED_API_KEY", "label": "向量嵌入 API 密钥", "desc": "硅基流动嵌入模型密钥", "url": "https://siliconflow.cn", "url_desc": "注册 → API Keys → 复制"},
        ]
        OPTIONAL_KEYS = [
            {"key": "WEBUI_PASSWORD", "label": "Web UI 密码", "desc": "留空则无需密码登录", "url": "", "url_desc": ""},
            {"key": "TAVILY_API_KEY", "label": "Tavily 搜索 API 密钥", "desc": "AI 搜索引擎", "url": "https://tavily.com", "url_desc": "注册 → API Keys"},
            {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow API 密钥", "desc": "硅基流动 API 密钥", "url": "https://siliconflow.cn", "url_desc": "注册 → API Keys"},
            {"key": "OPENROUTER_API_KEY", "label": "OpenRouter API 密钥", "desc": "OpenRouter API 密钥", "url": "https://openrouter.ai", "url_desc": "注册 → API Keys"},
            {"key": "WOLFRAMALPHA_API_KEY", "label": "WolframAlpha 知识计算密钥", "desc": "知识计算引擎", "url": "https://products.wolframalpha.com/api/", "url_desc": "注册 → Get AppID"},
            {"key": "AGNES_API_KEY", "label": "Agnes AI 图像/视频密钥", "desc": "图片生成和视频生成的核心依赖", "url": "https://agnes-ai.com", "url_desc": "注册 → API Keys"},
            {"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "GitHub 个人访问令牌", "desc": "GitHub MCP Server 所需", "url": "https://github.com/settings/tokens", "url_desc": "Generate new token"},
            {"key": "MODELSCOPE_ACCESS_TOKEN", "label": "魔搭 Access Token", "desc": "魔搭 ModelScope 免费模型发现", "url": "https://modelscope.cn", "url_desc": "注册 → 个人中心 → 访问令牌"},
        ]
        _load_env_values = lambda: {}
        _mask_value = lambda v: (v[:4] + "****") if v and len(v) > 4 else (v[:1] + "****" if v else "")

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
            "masked_value": _mask_value(val) if val else "",
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
            "masked_value": _mask_value(val) if val else "",
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
    """测试 SiliconFlow 嵌入 API Key（EMBED_API_KEY）。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.siliconflow.cn/v1/embeddings",
                headers={"Authorization": f"Bearer {key_value}"},
                json={"model": "BAAI/bge-large-zh-v1.5", "input": "test"},
            )
            if resp.status_code == 200:
                return True, "SiliconFlow 嵌入 API Key 验证成功"
            return False, f"SiliconFlow 嵌入 API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "SiliconFlow 嵌入 API 请求超时"
    except Exception as e:
        return False, f"SiliconFlow 嵌入 API 请求失败: {e}"


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
                if query_result.get("error", {}).get("code") == 1:
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

    return False, "未知的 API Key 类型"


@router.post("/setup/test-key", response_model=Envelope[dict])
async def test_key(body: dict):
    """测试 API Key 是否有效。"""
    key_name = body.get("key_name", "")
    key_value = body.get("key_value", "")

    if not key_name or not key_value:
        return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 key_name 和 key_value"})

    extra = body.get("extra", {})
    success, message = await test_single_key(key_name, key_value, extra)

    return Envelope(data={"success": success, "message": message})


@router.post("/setup/keys", response_model=Envelope[dict])
async def save_keys(body: dict):
    """将提供的 Key-Value 写入 .env 文件。"""
    from setup_wizard import (
        ENV_PATH,
        ENV_EXAMPLE_PATH,
        REQUIRED_KEYS,
        _parse_env_lines,
        _write_env,
        _load_env_values,
    )

    updates = body.get("keys")
    if not updates or not isinstance(updates, dict):
        return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 keys 字段（dict）"})

    # 当 test_required=true 时，对必填 Key 逐一测试，全部通过才保存
    test_required = body.get("test_required", False)
    if test_required:
        failed: list[dict[str, str]] = []
        required_key_names = [item["key"] for item in REQUIRED_KEYS]
        for rk in required_key_names:
            rv = updates.get(rk, "").strip()
            if not rv:
                # 未提供的必填 Key 跳过测试（由后续逻辑判断）
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
        # QQBOT 组合测试去重：如果两个都失败了，只保留一条
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
            return Envelope(
                ok=False,
                error={
                    "code": "KEY_TEST_FAILED",
                    "message": "必填 Key 验证失败，未保存",
                    "failed_keys": deduped_failed,
                },
            )

    # 如果 .env 不存在，从 .env.example 复制
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
    _write_env(existing_lines, merged)

    # 自动注册免费模型平台为自定义 Provider
    _auto_register_providers(updates)

    logger.info("setup.keys_saved count={}", len(updates))

    # 重新加载环境变量，使新配置立即生效
    import os
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)

    # 尝试重新初始化 core（从降级模式恢复）
    try:
        from web.server import app
        if hasattr(app, "state") and hasattr(app.state, "core"):
            core = app.state.core
            if not core._initialized:
                logger.info("setup.reinitializing_core")
                await core.init()
                if core._initialized:
                    from web.server import _apply_model_overrides
                    await _apply_model_overrides(core)
                    logger.info("setup.core_reinitialized")
                    # 刷新 AgentRegistry（注册内置子代理）
                    try:
                        from web.agent_registry import AgentRegistry
                        registry = getattr(app.state, "agent_registry", None)
                        if registry:
                            await registry.load_persisted()
                            logger.info("setup.registry_refreshed")
                    except Exception as e:
                        logger.warning("setup.registry_refresh_failed error={}", str(e))
    except Exception as e:
        logger.warning("setup.core_reinit_failed error={}", str(e))

    return Envelope(data={"saved": list(updates.keys()), "need_restart": True})


# 已知免费模型平台 → Provider 映射
_KNOWN_PROVIDERS = {
    "SILICONFLOW_API_KEY": {
        "id": "siliconflow",
        "label": "SiliconFlow 硅基流动",
        "format": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
    },
    "OPENROUTER_API_KEY": {
        "id": "openrouter",
        "label": "OpenRouter",
        "format": "openai",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "MODELSCOPE_ACCESS_TOKEN": {
        "id": "modelscope",
        "label": "ModelScope 魔搭",
        "format": "openai",
        "base_url": "https://api-inference.modelscope.cn/v1",
    },
    "AGNES_API_KEY": {
        "id": "agnes",
        "label": "Agnes AI",
        "format": "openai",
        "base_url": "https://apihub.agnes-ai.com/v1",
    },
}


def _auto_register_providers(updates: dict) -> None:
    """当用户配置了免费模型平台的 Key，自动注册为自定义 Provider。"""
    import os
    from web.config_service import get_config_service
    from web.custom_providers import register_into_router

    cfg = get_config_service()
    existing = cfg.get("models.providers", {}) or {}

    for env_key, provider_info in _KNOWN_PROVIDERS.items():
        api_key = updates.get(env_key, "").strip()
        if not api_key:
            continue

        pid = provider_info["id"]

        # 写入凭证文件
        from web.routers.models import _key_file
        from pathlib import Path
        cred_dir = Path(__file__).resolve().parent.parent.parent / "credentials"
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
                "base_url": provider_info["base_url"],
                "default_model": "",
                "enabled": True,
            }
            cfg.set(f"models.providers.{pid}", record)
            logger.info("setup.auto_provider_registered id={}", pid)

        # 注册到运行时 router
        try:
            from model_router import ModelRouter
            # 尝试获取 router 实例
            import web.server as srv
            # 延迟导入，server 可能还在初始化
        except Exception:
            pass

        # 通过 app.state 注册（如果 app 已启动）
        try:
            from web.server import app
            if hasattr(app, "state") and hasattr(app.state, "core"):
                router_obj = app.state.core.router
                register_into_router(
                    router_obj, pid,
                    provider_info["format"],
                    provider_info["base_url"],
                    api_key,
                )
                logger.info("setup.auto_provider_runtime id={}", pid)
        except Exception as e:
            logger.debug("setup.auto_provider_runtime_skip error={}", str(e))
