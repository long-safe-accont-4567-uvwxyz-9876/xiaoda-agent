"""市场清单模型 + 在线获取与缓存

数据源：
- ModelScope Skills API：插件与技能（modelscope.cn/openapi/v1/skills）
- MCP Hub 中国 (mcp-cn.com)：MCP 工具
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

# 在线数据源（国内可访问，合法合规）
MODELSCOPE_SKILLS_API = "https://www.modelscope.cn/openapi/v1/skills"
MCP_HUB_API = "https://www.mcp-cn.com/api/servers"

# 本地缓存路径
_CACHE_DIR = Path(__file__).parent / ".cache"
_CACHE_TTL = 3600  # 1 小时

# ModelScope 实际分类（基于 API 返回数据）
_PLUGIN_CATEGORIES = [
    "developer-tools", "code-quality-testing", "frontend-development",
    "mobile-development", "cloud-devops",
]
_SKILL_CATEGORIES = [
    "skill-management", "ai-media", "ai-automation",
    "doc-processing", "marketing-seo", "other",
]


class MarketItem(BaseModel):
    """市场中的单个条目（插件、MCP 工具或技能）"""
    id: str = Field(description="唯一标识")
    type: Literal["skill", "mcp", "plugin"] = Field(description="类型")
    name: str = Field(description="显示名称")
    description: str = Field(default="", description="描述")
    version: str = Field(default="0.1.0", description="版本号")
    author: str = Field(default="", description="作者")
    tags: list[str] = Field(default_factory=list, description="标签")
    icon: str = Field(default="", description="图标 URL 或 emoji")
    download_url: str = Field(default="", description="下载地址")
    sha256: str = Field(default="", description="文件 SHA256 校验和")
    min_agent_version: str = Field(default="", description="最低 agent 版本要求")
    homepage: str = Field(default="", description="项目主页")
    license: str = Field(default="", description="许可证")
    # MCP 专属字段
    qualified_name: str = Field(default="", description="MCP 包名")
    use_count: int = Field(default=0, description="使用次数")
    connections: str = Field(default="", description="MCP 连接配置")
    # 内置内容（技能专用）
    content: str = Field(default="", description="内置内容")
    content_type: str = Field(default="", description="内容类型：python / markdown")


class MarketManifest(BaseModel):
    """市场清单"""
    version: str = Field(default="1.0", description="清单格式版本")
    updated: str = Field(default="", description="更新日期")
    source: str = Field(default="", description="数据来源")
    items: list[MarketItem] = Field(default_factory=list)


class ManifestFetcher:
    """获取并缓存市场清单：在线 API → 本地缓存"""

    def __init__(self, source: str = "modelscope_plugins",
                 cache_name: str = "manifest",
                 categories: list[str] | None = None,
                 item_type: str = "plugin") -> None:
        self._source = source
        self._cache_name = cache_name
        self._categories = categories or []
        self._item_type = item_type
        self._cache: MarketManifest | None = None
        self._cache_time: float = 0
        self._cache_file = _CACHE_DIR / f"{cache_name}.json"

    async def fetch(self, force: bool = False) -> MarketManifest:
        """获取清单：内存缓存 → 在线 API → 本地缓存"""
        now = time.time()
        if not force and self._cache and (now - self._cache_time) < _CACHE_TTL:
            return self._cache

        # 在线 API
        manifest = await self._fetch_online()
        if manifest and manifest.items:
            self._cache = manifest
            self._cache_time = now
            self._save_local(manifest)
            logger.info("market.manifest_online", source=self._source,
                        items=len(manifest.items))
            return manifest

        # 本地缓存兜底（网络不可用时）
        manifest = self._load_local()
        if manifest and manifest.items:
            self._cache = manifest
            self._cache_time = now
            logger.info("market.manifest_cached", items=len(manifest.items))
            return manifest

        logger.warning("market.manifest_empty", source=self._source)
        return MarketManifest()

    async def _fetch_online(self) -> MarketManifest | None:
        """从在线 API 获取清单"""
        try:
            if self._source == "mcp_hub":
                return await self._fetch_mcp_hub()
            elif self._source == "modelscope":
                return await self._fetch_modelscope()
        except Exception as e:
            logger.warning("market.online_fetch_failed", source=self._source,
                           error=str(e))
        return None

    async def _fetch_modelscope(self) -> MarketManifest | None:
        """从 ModelScope Skills API 获取插件/技能列表

        注意：API 的 category 参数不生效（返回全部结果），
        因此不限制 category 参数，改为本地过滤 + 限制最大页数防止超时。
        """
        try:
            import httpx
            all_items: list[MarketItem] = []
            page = 1
            page_size = 20  # API 最大 20
            max_pages = 10  # 最多取 10 页（200 条），避免遍历 75000+ 条超时

            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                while page <= max_pages:
                    params: dict[str, Any] = {
                        "page_number": page,
                        "page_size": page_size,
                    }

                    resp = await client.get(MODELSCOPE_SKILLS_API, params=params)
                    resp.raise_for_status()
                    body = resp.json()

                    if not body.get("success"):
                        logger.warning("market.modelscope_api_error", body=str(body)[:200])
                        break

                    data = body.get("data", {})
                    skills = data.get("skills", [])
                    if not skills:
                        break

                    for skill in skills:
                        skill_categories = skill.get("category", "")
                        if isinstance(skill_categories, str):
                            skill_categories = [c.strip() for c in skill_categories.split(",") if c.strip()]
                        elif not isinstance(skill_categories, list):
                            skill_categories = []

                        # 按分类过滤
                        if self._categories:
                            if not any(c in self._categories for c in skill_categories):
                                continue

                        skill_id = skill.get("id", "")
                        item = MarketItem(
                            id=f"{self._item_type}-{skill_id}",
                            type=self._item_type,
                            name=skill.get("display_name", str(skill_id)),
                            description=skill.get("description", ""),
                            author=skill.get("developer", ""),
                            tags=skill.get("tags", []) or [],
                            icon=skill.get("logo_url", "") or "",
                            homepage=skill.get("source_url", "") or "",
                            download_url=skill.get("source_url", "") or "",
                            license=skill.get("license", "") or "",
                            use_count=skill.get("downloads", 0) or skill.get("view_count", 0),
                        )
                        all_items.append(item)

                    total = data.get("total", 0)
                    logger.debug("market.modelscope_page", page=page,
                                 matched=len(all_items), total=total)
                    if page * page_size >= total:
                        break
                    page += 1

            if all_items:
                return MarketManifest(
                    version="1.0",
                    updated=time.strftime("%Y-%m-%d"),
                    source="modelscope.cn",
                    items=all_items,
                )
            logger.warning("market.modelscope_no_match",
                           categories=self._categories, pages_tried=page)
        except Exception as e:
            logger.warning("market.modelscope_fetch_failed", error=str(e))
        return None

    async def _fetch_mcp_hub(self) -> MarketManifest | None:
        """从 MCP Hub 中国 API 获取 MCP 工具列表"""
        try:
            import httpx
            all_items: list[MarketItem] = []
            page = 1
            page_size = 50

            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                while True:
                    resp = await client.get(
                        MCP_HUB_API,
                        params={"page": page, "pageSize": page_size}
                    )
                    resp.raise_for_status()
                    body = resp.json()

                    if body.get("code") != 0:
                        logger.warning("market.mcp_hub_api_error", body=body)
                        break

                    servers = body.get("data", [])
                    if not servers:
                        break

                    for srv in servers:
                        tag_str = srv.get("tag", "")
                        tags = [t.strip() for t in tag_str.split(",") if t.strip()] if tag_str else []

                        qualified = srv.get("qualified_name", "")
                        detail_url = f"https://www.mcp-cn.com/server/{srv.get('server_id', '')}"

                        item = MarketItem(
                            id=f"mcp-{srv.get('server_id', qualified)}",
                            type="mcp",
                            name=srv.get("display_name", qualified),
                            description=srv.get("description", ""),
                            author=srv.get("creator", ""),
                            tags=tags,
                            icon=srv.get("logo", "") or "",
                            homepage=detail_url,
                            qualified_name=qualified,
                            use_count=srv.get("use_count", 0),
                            connections=srv.get("connections", ""),
                        )
                        all_items.append(item)

                    pagination = body.get("pagination", {})
                    total = pagination.get("total", 0)
                    if page * page_size >= total:
                        break
                    page += 1

            if all_items:
                return MarketManifest(
                    version="1.0",
                    updated=time.strftime("%Y-%m-%d"),
                    source="mcp-cn.com",
                    items=all_items,
                )
        except Exception as e:
            logger.warning("market.mcp_hub_fetch_failed", error=str(e))
        return None

    def _save_local(self, manifest: MarketManifest) -> None:
        """保存到本地缓存"""
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.debug("market.cache_save_failed", error=str(e))

    def _load_local(self) -> MarketManifest | None:
        """从本地缓存加载"""
        try:
            if self._cache_file.exists():
                data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                return MarketManifest(**data)
        except Exception as e:
            logger.debug("market.cache_load_failed", error=str(e))
        return None


# 全局单例
_plugins_fetcher: ManifestFetcher | None = None
_skills_fetcher: ManifestFetcher | None = None
_mcp_fetcher: ManifestFetcher | None = None


def get_plugins_fetcher() -> ManifestFetcher:
    """获取插件清单 Fetcher（ModelScope Skills API，插件相关分类）"""
    global _plugins_fetcher
    if _plugins_fetcher is None:
        _plugins_fetcher = ManifestFetcher(
            source="modelscope",
            cache_name="plugins",
            categories=_PLUGIN_CATEGORIES,
            item_type="plugin",
        )
    return _plugins_fetcher


def get_skills_fetcher() -> ManifestFetcher:
    """获取技能清单 Fetcher（ModelScope Skills API，技能相关分类）"""
    global _skills_fetcher
    if _skills_fetcher is None:
        _skills_fetcher = ManifestFetcher(
            source="modelscope",
            cache_name="skills",
            categories=_SKILL_CATEGORIES,
            item_type="skill",
        )
    return _skills_fetcher


def get_mcp_fetcher() -> ManifestFetcher:
    """获取 MCP 工具清单 Fetcher（MCP Hub 中国 API）"""
    global _mcp_fetcher
    if _mcp_fetcher is None:
        _mcp_fetcher = ManifestFetcher(
            source="mcp_hub",
            cache_name="mcp",
            item_type="mcp",
        )
    return _mcp_fetcher
