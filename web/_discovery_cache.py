"""模型发现缓存 —— 从 web.routers.model_discovery 抽取.

原 web.routers.models 顶层 `from web.routers.model_discovery import invalidate_discovery_cache`,
而 model_discovery 函数内又 `from web.routers.models import load_provider_key`, 形成:
    web.routers.models <-> web.routers.model_discovery

将缓存 (_cache / _CACHE_TTL) 与失效函数 (invalidate_discovery_cache) 抽到本模块,
该模块不依赖任何 web.routers 或 model_router, 从而打破循环.
"""

import asyncio

_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL = 30 * 60
_cache_lock = asyncio.Lock()


async def invalidate_discovery_cache() -> None:
    """清除模型发现缓存，使下次请求重新获取。"""
    async with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0.0
