"""setup 向导 API Key 探针库 —— 自 web/routers/setup.py 字节搬移（2026-08-22 拆分蓝图 P1）。

内容：14 个 provider 专属 ``_test_*`` 探针 + Bearer 通用模板 + catalog/名称
两级分发 + ``test_single_key`` 聚合入口。纯函数库：无路由、无全局状态，
被 setup.py 的 /setup/test-key 端点与 save_keys 必填校验消费。

兼容契约：setup.py 同名 re-export，既有 from web.routers.setup import
test_single_key 用法不受影响。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
from config_providers import get_base_url_for_provider, get_default_model_for_provider
from loguru import logger

# 探针超时（随块搬移，原 setup.py 模块常量）
_TIMEOUT = 10.0

async def _test_get_with_bearer(key_value: str, url: str, name: str,
                                 success_msg: str | None = None) -> tuple[bool, str]:
    """GET + Bearer 头的通用验证模板。

    适用 deepseek/openrouter/agnes/modelscope/github 等「GET models 端点 → 200/401」
    的 provider。成功消息默认 ``{name} API Key 验证成功``。
    保留 wolframalpha（特殊 queryresult 解析）与 ollama/llama_cpp（URL 规范化 + SSRF）独立实现。
    """
    success_msg = success_msg or f"{name} API Key 验证成功"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {key_value}"},
            )
            if resp.status_code == 200:
                return True, success_msg
            if resp.status_code == 401:
                return False, f"{name} API Key 无效或已过期"
            return False, f"{name} API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, f"{name} API 请求超时"
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"{name} API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_get_with_bearer.unexpected_error")
        return False, f"{name} API 请求失败: {e}"


async def _test_mimo(key_value: str) -> tuple[bool, str]:
    """测试 MiMo API Key。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{get_base_url_for_provider('mimo').rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key_value}"},
                json={
                    "model": get_default_model_for_provider("mimo"),
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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"MiMo API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_mimo.unexpected_error")
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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"QQ Bot API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_qqbot.unexpected_error")
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
            if resp.status_code == 401:
                return False, "SiliconFlow API Key 无效或已过期"
            return False, f"SiliconFlow API 返回 HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "SiliconFlow API 请求超时"
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"SiliconFlow API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_siliconflow.unexpected_error")
        return False, f"SiliconFlow API 请求失败: {e}"


async def _test_openrouter(key_value: str) -> tuple[bool, str]:
    """测试 OpenRouter API Key。"""
    return await _test_get_with_bearer(
        key_value, "https://openrouter.ai/api/v1/models", "OpenRouter")


async def _test_deepseek(key_value: str) -> tuple[bool, str]:
    """测试 DeepSeek API Key。"""
    return await _test_get_with_bearer(
        key_value, "https://api.deepseek.com/v1/models", "DeepSeek")


async def _test_agnes(key_value: str) -> tuple[bool, str]:
    """测试 Agnes AI API Key。"""
    _agnes_url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.cn/v1")
    return await _test_get_with_bearer(
        key_value, f"{_agnes_url.rstrip('/')}/models", "Agnes AI")


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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"WolframAlpha API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_wolframalpha.unexpected_error")
        return False, f"WolframAlpha API 请求失败: {e}"


async def _test_modelscope(key_value: str) -> tuple[bool, str]:
    """测试 ModelScope Access Token（推理 API）。"""
    return await _test_get_with_bearer(
        key_value, "https://api-inference.modelscope.cn/v1/models",
        "ModelScope Access Token")


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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"Tavily API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_tavily.unexpected_error")
        return False, f"Tavily API 请求失败: {e}"


async def _test_github(key_value: str) -> tuple[bool, str]:
    """测试 GitHub Personal Access Token。"""
    # GitHub 需额外 Accept 头，无法复用 _test_get_with_bearer（其只发 Authorization）
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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"GitHub API 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_github.unexpected_error")
        return False, f"GitHub API 请求失败: {e}"


async def _test_ollama(base_url: str) -> tuple[bool, str]:
    """测试 Ollama 服务连通性。"""
    # URL 规范化：Ollama OpenAI 兼容端点需以 /v1 结尾
    import urllib.parse as _urlparse
    _parsed = _urlparse.urlparse(base_url)
    _path = _parsed.path.rstrip("/")
    if not _path.endswith("/v1"):
        base_url = f"{base_url.rstrip('/')}/v1"
    # SSRF 防护：校验 URL 不指向内网/元数据服务。
    # Ollama 是本地/容器内部署，允许 localhost / 127.0.0.1 / host.docker.internal
    from security.ssrf_guard import validate_url, is_local_host
    if not is_local_host(base_url):
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
            return False, f"Ollama 返回 HTTP {resp.status_code}，请确认 Ollama 已启动且 URL 正确（需 /v1 后缀）"
    except httpx.ConnectError:
        return False, f"无法连接到 Ollama 服务（{base_url}），请确认 Ollama 已启动"
    except httpx.TimeoutException:
        return False, "Ollama 连接超时"
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"Ollama 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_ollama.unexpected_error")
        return False, f"Ollama 请求失败: {e}"


async def _test_llama_cpp(base_url: str) -> tuple[bool, str]:
    """测试 llama.cpp server 连通性（OpenAI 兼容 /v1/models 端点）。"""
    # URL 规范化：OpenAI 兼容端点需以 /v1 结尾
    import urllib.parse as _urlparse
    _parsed = _urlparse.urlparse(base_url)
    _path = _parsed.path.rstrip("/")
    if not _path.endswith("/v1"):
        base_url = f"{base_url.rstrip('/')}/v1"
    # SSRF 防护：llama.cpp 是本地部署，允许 localhost / 127.0.0.1 / host.docker.internal
    from security.ssrf_guard import validate_url, is_local_host
    if not is_local_host(base_url):
        allowed, reason = validate_url(base_url)
        if not allowed:
            return False, f"URL 安全检查失败: {reason}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models")
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", [])
                return True, f"llama.cpp 可用，发现 {len(models)} 个模型"
            return False, f"llama.cpp 返回 HTTP {resp.status_code}，请确认 llama.cpp server 已启动且 URL 正确（需 /v1 后缀）"
    except httpx.ConnectError:
        return False, f"无法连接到 llama.cpp 服务（{base_url}），请确认 llama.cpp server 已启动"
    except httpx.TimeoutException:
        return False, "llama.cpp 连接超时"
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        return False, f"llama.cpp 请求失败: {e}"
    except Exception as e:
        logger.exception("setup._test_llama_cpp.unexpected_error")
        return False, f"llama.cpp 请求失败: {e}"


async def _test_key_by_catalog(key_name: str, key_value: str) -> tuple[bool, str] | None:
    """尝试通过 provider catalog 测试 key；不属于任何 provider 时返回 None。"""
    from config import get_provider_catalog
    catalog = get_provider_catalog()
    if key_name in {"MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_API_KEY"}:
        return await _test_modelscope(key_value)
    for definition in catalog.list():
        if key_name not in definition.auth.environment_aliases:
            continue
        from llm_gateway.provider_service import ProviderService
        draft = ProviderService._record(definition)
        report = await ProviderService(
            SimpleNamespace(get=lambda *args, **kwargs: {}),
            catalog,
            SimpleNamespace(_custom_clients={}),
        ).test(draft, {"api_key": key_value})
        return report.available, "Provider 凭证验证成功" if report.available else (report.error or "Provider 凭证验证失败")
    return None


async def _test_key_by_name(key_name: str, key_value: str, extra: dict) -> tuple[bool, str]:
    """根据 key_name 调用对应的测试函数。"""
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
    if key_name == "TAVILY_API_KEY":
        return await _test_tavily(key_value)
    if key_name == "GITHUB_PERSONAL_ACCESS_TOKEN":
        return await _test_github(key_value)
    if key_name == "OLLAMA_BASE_URL":
        return await _test_ollama(key_value)
    if key_name == "LLAMA_CPP_BASE_URL":
        return await _test_llama_cpp(key_value)
    if key_name in {"WEBUI_PASSWORD"}:
        return True, "配置已保存"
    return False, "未知的 API Key 类型"


async def test_single_key(key_name: str, key_value: str, extra: dict | None = None) -> tuple[bool, str]:
    """根据 key_name 调用对应的测试函数，返回 (success, message)。"""
    extra = extra or {}
    catalog_result = await _test_key_by_catalog(key_name, key_value)
    if catalog_result is not None:
        return catalog_result
    return await _test_key_by_name(key_name, key_value, extra)

