"""AgentRegistry — Agent 的 CRUD、持久化与权限矩阵（R5/R6/R7）。

内置 4 个子代理由 core/bootstrap.py 注册；本模块负责：
- 加载/保存 config/agents/*.json 中的用户自建 Agent 与对内置 Agent 的覆盖
- 运行时热插拔（dispatcher.register / unregister）
- 权限矩阵读写（excluded_tools / mcp_servers，改完即时生效，
  因为 SubAgent._filtered_tools() 每次对话时实时计算）
"""
from __future__ import annotations
from typing import Any

import dataclasses
import json
import time
from pathlib import Path

from loguru import logger

# frozen 模式下使用用户目录（~/.ai-agent/data/config/agents/），避免写入 _MEIPASS 只读目录
from config import AGENTS_CONFIG_DIR, DEFAULT_PROVIDER, _FALLBACK_BASE


def _resolve_personality_path(pf: str) -> str | None:
    """将 personality_file 相对路径解析为绝对路径。

    personality_file 路径（如 "config/agents/xiaoli_personality.md"）相对于项目源码根目录。
    - dev 模式: _FALLBACK_BASE = 项目根目录
    - frozen 模式: 先找 _MEIPASS（打包内），再找 _FALLBACK_BASE（用户目录）
    - 最终 fallback: 在 AGENTS_DIR 下查找
    """
    candidates = []
    # 1. 项目源码根目录（dev 模式下的正确位置）
    candidates.append(_FALLBACK_BASE / pf)
    # 2. PyInstaller 打包目录（frozen 模式）
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(Path(meipass) / pf)
    # 3. 用户数据目录（之前可能保存过的位置）
    candidates.append(AGENTS_DIR / pf)
    # 4. 用户数据目录 + 文件名（fallback：只取文件名部分）
    candidates.append(AGENTS_DIR / Path(pf).name)

    for c in candidates:
        if c.exists():
            return str(c)
    return None
import config as _config
AGENTS_DIR = AGENTS_CONFIG_DIR
BUILTIN_AGENTS = {"xiaoli", "xiaolang", "xiaolian", "xiaoke"}

# 内置 Agent 的 excluded_tools（与 core/bootstrap.py 中 _register_sub_agents 保持一致）
# 用于降级模式下 _builtin_stub() 计算实际 tool_count
BUILTIN_EXCLUDED_TOOLS: dict[str, set[str]] = {
    "xiaoli": {"call_xiaoli", "shell_command", "python_executor", "write_file",
             "search_files", "read_file", "list_files", "web_browse",
             "document_reader", "multi_search", "wolfram_query"},
    "xiaolang": {"call_xiaoli", "call_xiaoda"},
    "xiaolian": {"call_xiaoli", "call_xiaoda", "shell_command", "python_executor", "write_file"},
    "xiaoke": {"call_xiaoli", "call_xiaoda", "shell_command", "write_file"},
}
# 内置 Agent 默认背景板（打包在前端 dist/assets/wallpapers/ 下）
DEFAULT_WALLPAPERS = {
    "xiaoda": "/assets/webui_background.jpg",
    "xiaoli": "/assets/wallpapers/xiaoli.jpg",
    "xiaolang": "/assets/wallpapers/xiaolang.jpg",
    "xiaolian": "/assets/wallpapers/xiaolian.jpg",
    "xiaoke": "/assets/wallpapers/xiaoke.jpg",
}
# 主体纳西妲不是 SubAgent，但要出现在 Agent 列表里供切换
MAIN_AGENT_META = {
    "name": "xiaoda",
    "display_name": "小妲",
    "display_name_en": "Xiao Da",
    "builtin": True,
    "is_main": True,
    "enabled": True,
    "provider": DEFAULT_PROVIDER,
    "wallpaper": DEFAULT_WALLPAPERS["xiaoda"],
    "voice_ref": None,
    "route_description": "主体，默认对话对象，可委托其他子代理",
}

# 配置文件中允许的字段（与 SubAgentConfig 对齐）
_CONFIG_FIELDS = [
    "name", "display_name", "display_name_en", "provider", "model", "personality_file", "voice_ref",
    "excluded_tools", "base_url", "api_key_env", "capabilities", "route_description",
    "mcp_servers", "max_spawn_depth", "max_turns", "effort", "permission_mode",
    "memory_scope", "background", "wallpaper",
    "allowed_paths", "forbidden_paths",
]


class AgentRegistry:
    """Agent 注册表，负责 Agent 的 CRUD、持久化与权限管理。"""

    def __init__(self, core: Any) -> None:
        """初始化 Agent 注册表。"""
        self.core = core
        self._disabled: set[str] = set()
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 持久化 ──────────────────────────────────────────

    def _file(self, name: str) -> Path:
        """返回指定 Agent 的配置文件路径。"""
        return AGENTS_DIR / f"{name}.json"

    def _personality_file(self, name: str) -> Path:
        """返回指定 Agent 的人格文件路径。"""
        return AGENTS_DIR / f"{name}_personality.md"

    def _save_config(self, cfg: Any) -> None:
        """将 Agent 配置持久化到 JSON 文件。"""
        data = {}
        for f in _CONFIG_FIELDS:
            v = getattr(cfg, f, None)
            if isinstance(v, set):
                v = sorted(v)
            data[f] = v
        data["_saved_at"] = time.time()
        self._file(cfg.name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 小妲主体配置（excluded_tools / mcp_servers） ──

    def _xiaoda_cfg_path(self) -> Path:
        return AGENTS_DIR / "xiaoda.json"

    def _load_xiaoda_cfg(self) -> dict:
        fp = self._xiaoda_cfg_path()
        if fp.exists():
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("xiaoda.json 损坏，忽略")
        return {}

    def _save_xiaoda_cfg(self, data: dict) -> None:
        data["_saved_at"] = time.time()
        self._xiaoda_cfg_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_xiaoda_field(self, key: str, value) -> None:
        """保存小妲单个字段到 xiaoda.json。"""
        cfg = self._load_xiaoda_cfg()
        cfg[key] = value
        self._save_xiaoda_cfg(cfg)

    # 旧版 agent 名称，升级后不应被当作自定义 agent 注册
    _DEPRECATED_AGENT_NAMES = {"nahida", "keli", "yinlang", "xilian", "nike"}

    async def load_persisted(self) -> None:
        """启动时调用：恢复自建 Agent 并应用对内置 Agent 的覆盖。"""
        from agent_dispatcher import SubAgentConfig
        for fp in sorted(AGENTS_DIR.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                name = data.get("name", "")
                if not name:
                    continue
                # 跳过旧版 agent 名称，防止残留配置被当作自定义 agent 注册
                if name in self._DEPRECATED_AGENT_NAMES:
                    logger.info("agent_registry.deprecated_skip name={} file={}", name, fp.name)
                    continue
                if name in BUILTIN_AGENTS:
                    agent = self.core.dispatcher.get_agent(name)
                    if agent:
                        old_provider = agent.config.provider
                        old_model = agent.config.model
                        old_base_url = agent.config.base_url
                        self._apply_fields(agent.config, data)
                        # 如果 provider/model/base_url 变了，重建客户端
                        if (agent.config.provider != old_provider
                                or agent.config.model != old_model
                                or agent.config.base_url != old_base_url):
                            await agent.reload_model_config(
                                agent.config.provider,
                                agent.config.model,
                                agent.config.base_url,
                                agent.config.api_key_env,
                            )
                        logger.info("agent_registry.builtin_override name={}", name)
                else:
                    kwargs = {f: data[f] for f in _CONFIG_FIELDS if f in data and data[f] is not None}
                    kwargs["excluded_tools"] = set(kwargs.get("excluded_tools") or [])
                    # 修正 personality_file 相对路径
                    pf = kwargs.get("personality_file")
                    if pf:
                        abs_pf = _resolve_personality_path(pf)
                        if abs_pf:
                            kwargs["personality_file"] = abs_pf
                    cfg = SubAgentConfig(**kwargs)
                    await self.core.dispatcher.register(cfg)
                    logger.info("agent_registry.custom_loaded name={}", name)
                if data.get("_disabled"):
                    self._disabled.add(name)
            except Exception as e:
                logger.warning("agent_registry.load_failed file={} error={}", fp.name, str(e))

    @staticmethod
    def _apply_fields(cfg: Any, data: dict) -> None:
        """将配置数据中的字段应用到 AgentConfig 对象上。"""
        for f in _CONFIG_FIELDS:
            if f in ("name",) or f not in data or data[f] is None:
                continue
            v = data[f]
            if f == "excluded_tools":
                v = set(v)
            setattr(cfg, f, v)
        # 修正 personality_file 相对路径 → 绝对路径
        pf = getattr(cfg, "personality_file", None)
        if pf and not Path(pf).is_absolute():
            abs_pf = _resolve_personality_path(pf)
            if abs_pf:
                cfg.personality_file = abs_pf

    # ── 查询 ────────────────────────────────────────────

    # ── 内置 Agent 桩数据（降级模式下 dispatcher 未注册时使用）──

    _BUILTIN_STUBS: dict[str, dict] = {
        "xiaoli": {
            "display_name": "小莉", "display_name_en": "Xiaoli",
            "provider": "default", "model": "default",
            "route_description": "日常聊天、玩耍、轻松有趣的对话",
            "capabilities": ["chat", "play", "fun"],
        },
        "xiaolang": {
            "display_name": "小狼", "display_name_en": "Xiaolang",
            "provider": "default", "model": "default",
            "route_description": "编程、代码编写、调试、技术问题、硬件控制、系统运维、开发辅助",
            "capabilities": ["coding", "debug", "script", "programming", "hardware", "system", "devops"],
        },
        "xiaolian": {
            "display_name": "小涟", "display_name_en": "Xiaolian",
            "provider": "default", "model": "default",
            "route_description": "搜索信息、查询资料、探索发现",
            "capabilities": ["search", "lookup", "query", "explore", "discover"],
        },
        "xiaoke": {
            "display_name": "小可", "display_name_en": "Xiaoke",
            "provider": "default", "model": "default",
            "route_description": "研究分析、学术思考、深度解读",
            "capabilities": ["research", "analysis", "study", "academic"],
        },
    }

    def _builtin_stub(self, name: str) -> dict:
        """生成内置 Agent 的桩数据（降级模式下使用）。"""
        stub = self._BUILTIN_STUBS.get(name, {})
        excluded = BUILTIN_EXCLUDED_TOOLS.get(name, set())
        blocked = self._blocked()
        # 实际 tool_count = 全部工具 - 该 agent 排除的工具 - 子代理禁用工具
        tool_count = len([t for t in self._all_tool_names()
                          if t not in excluded and t not in blocked])
        return {
            "name": name,
            "display_name": stub.get("display_name", name),
            "display_name_en": stub.get("display_name_en", name),
            "builtin": True,
            "is_main": False,
            "enabled": True,
            "provider": _config.DEFAULT_PROVIDER if stub.get("provider") == "default" else stub.get("provider", ""),
            "model": stub.get("model", ""),
            "base_url": "",
            "api_key_env": "",
            "voice_ref": None,
            "route_description": stub.get("route_description", ""),
            "capabilities": stub.get("capabilities", []),
            "excluded_tools": sorted(excluded),
            "mcp_servers": [],
            "max_turns": 8,
            "effort": "medium",
            "permission_mode": "default",
            "memory_scope": "shared",
            "background": None,
            "wallpaper": DEFAULT_WALLPAPERS.get(name, ""),
            "allowed_paths": [],
            "forbidden_paths": [],
            "tool_count": tool_count,
            "degraded": True,
        }

    def list(self) -> list[dict]:
        """列出所有 Agent（主体 + 已注册子代理 + 降级桩）。"""
        main = dict(MAIN_AGENT_META,
                    model=self._main_model(),
                    provider=_config.DEFAULT_PROVIDER,
                    tool_count=len(self._all_tool_names()),
                    mcp_servers=[])
        # 加载小妲持久化的 voice_ref / display_name
        xiaoda_cfg = self._load_xiaoda_cfg()
        if "voice_ref" in xiaoda_cfg:
            main["voice_ref"] = xiaoda_cfg["voice_ref"]
        if xiaoda_cfg.get("display_name"):
            main["display_name"] = xiaoda_cfg["display_name"]
            MAIN_AGENT_META["display_name"] = xiaoda_cfg["display_name"]
        if xiaoda_cfg.get("display_name_en"):
            main["display_name_en"] = xiaoda_cfg["display_name_en"]
            MAIN_AGENT_META["display_name_en"] = xiaoda_cfg["display_name_en"]
        try:
            from web.config_service import get_config_service
            wp = get_config_service().get("ui.main_wallpaper")
            if wp:
                main["wallpaper"] = wp
        except Exception:
            logger.debug("registry.wallpaper_error", exc_info=True)
        out = [main]
        registered_names: set[str] = set()
        for info in self.core.dispatcher.list_agents():
            name = info.get("name", "")
            registered_names.add(name)
            agent = self.core.dispatcher.get_agent(name)
            if not agent:
                continue
            cfg = agent.config
            out.append(self._serialize(cfg, enabled=name not in self._disabled,
                                       degraded=getattr(agent, "degraded", False)))
        # 降级模式下 dispatcher 未注册内置 Agent，用桩数据补齐
        for name in BUILTIN_AGENTS:
            if name not in registered_names:
                out.append(self._builtin_stub(name))
        return out

    def _main_model(self) -> str:
        """获取主体使用的模型名称。"""
        try:
            from model_router import ROUTE_TABLE
            return ROUTE_TABLE.get("chat", {}).get("model", "")
        except Exception:
            logger.debug("registry.model_name_error", exc_info=True)
            return ""

    def _serialize(self, cfg: Any, enabled: bool = True, degraded: bool = False) -> dict:
        """将 AgentConfig 序列化为 API 返回用的字典。"""
        excluded = set(cfg.excluded_tools or set())
        tool_count = len([t for t in self._all_tool_names()
                          if t not in excluded and t not in self._blocked()])
        return {
            "name": cfg.name,
            "display_name": cfg.display_name,
            "display_name_en": getattr(cfg, "display_name_en", "") or cfg.name,
            "builtin": cfg.name in BUILTIN_AGENTS,
            "is_main": False,
            "enabled": enabled,
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "api_key_env": cfg.api_key_env,
            "voice_ref": cfg.voice_ref,
            "capabilities": list(cfg.capabilities or []),
            "route_description": cfg.route_description,
            "excluded_tools": sorted(excluded),
            "mcp_servers": list(cfg.mcp_servers or []),
            "max_turns": cfg.max_turns,
            "effort": cfg.effort,
            "permission_mode": cfg.permission_mode,
            "memory_scope": cfg.memory_scope,
            "background": cfg.background,
            "wallpaper": getattr(cfg, "wallpaper", "") or DEFAULT_WALLPAPERS.get(cfg.name, ""),
            "allowed_paths": list(getattr(cfg, "allowed_paths", []) or []),
            "forbidden_paths": list(getattr(cfg, "forbidden_paths", []) or []),
            "tool_count": tool_count,
            "degraded": degraded,
        }

    def get(self, name: str) -> dict | None:
        """根据名称获取单个 Agent 的信息。"""
        if name == "xiaoda":
            return self.list()[0]
        agent = self.core.dispatcher.get_agent(name)
        if agent:
            return self._serialize(agent.config, enabled=name not in self._disabled,
                                   degraded=getattr(agent, "degraded", False))
        # 降级模式：返回桩数据
        if name in BUILTIN_AGENTS:
            return self._builtin_stub(name)
        return None

    # ── 增删改 ──────────────────────────────────────────

    async def create(self, data: dict) -> dict:
        """创建新的子代理，注册到 dispatcher 并持久化配置。"""
        from agent_dispatcher import SubAgentConfig
        name = (data.get("name") or "").strip().lower()
        if not name or not name.isidentifier():
            raise ValueError("name 必须是合法标识符（小写字母/数字/下划线）")
        if name == "xiaoda" or self.core.dispatcher.get_agent(name):
            raise ValueError(f"Agent {name} 已存在")
        personality_text = data.pop("personality_text", "")
        kwargs = {f: data[f] for f in _CONFIG_FIELDS if f in data and data[f] is not None}
        kwargs["name"] = name
        kwargs["excluded_tools"] = set(kwargs.get("excluded_tools") or [])
        if personality_text:
            from config import reverse_agent_name_replacements
            personality_text = reverse_agent_name_replacements(personality_text)
            pf = self._personality_file(name)
            pf.write_text(personality_text, encoding="utf-8-sig")
            kwargs["personality_file"] = str(pf)
        cfg = SubAgentConfig(**kwargs)
        ok = await self.core.dispatcher.register(cfg)
        if not ok:
            raise ValueError("dispatcher 注册失败（检查 provider/api_key_env）")
        self._save_config(cfg)
        return self._serialize(cfg)

    async def update(self, name: str, data: dict) -> dict:
        """更新 Agent 配置，必要时热重载模型客户端并持久化。"""
        # 主体小妲特殊处理：不在 dispatcher 中，只更新壁纸/人格/voice_ref/display_name
        if name == "xiaoda":
            if data.get("wallpaper"):
                from web.config_service import get_config_service
                get_config_service().set("ui.main_wallpaper", data["wallpaper"])
            personality_text = data.pop("personality_text", None)
            if personality_text is not None:
                from config import reverse_agent_name_replacements, WORKSPACE_DIR
                personality_text = reverse_agent_name_replacements(personality_text)
                soul_path = WORKSPACE_DIR / "SOUL.md"
                soul_path.write_text(personality_text, encoding="utf-8-sig")
            # voice_ref 更新
            if "voice_ref" in data:
                self._save_xiaoda_field("voice_ref", data["voice_ref"])
            # display_name 更新：持久化 + 同步 MAIN_AGENT_META，使全局立即可见
            if "display_name" in data and data["display_name"]:
                self._save_xiaoda_field("display_name", data["display_name"])
                MAIN_AGENT_META["display_name"] = data["display_name"]
            # display_name_en 更新：持久化 + 同步 MAIN_AGENT_META
            if "display_name_en" in data and data["display_name_en"]:
                self._save_xiaoda_field("display_name_en", data["display_name_en"])
                MAIN_AGENT_META["display_name_en"] = data["display_name_en"]
            return self.get("xiaoda")
        agent = self._require(name)
        personality_text = data.pop("personality_text", None)
        # 记录旧值，用于判断是否需要热重载客户端
        old_provider = agent.config.provider
        old_model = agent.config.model
        # 当 provider 变更时，自动解析 base_url/api_key_env，避免不一致配置
        new_provider = data.get("provider")
        if new_provider and new_provider != old_provider:
            try:
                base_url, api_key_env = self._resolve_provider_info(new_provider)
                data.setdefault("base_url", base_url)
                data.setdefault("api_key_env", api_key_env)
            except ValueError:
                logger.debug("unknown provider={}, skip auto-fill base_url/api_key_env", new_provider, exc_info=True)
        self._apply_fields(agent.config, data)
        # provider 或 model 变更后，尝试热重载客户端
        provider_changed = new_provider is not None and new_provider != old_provider
        model_changed = "model" in data and data["model"] != old_model
        if provider_changed or model_changed:
            try:
                await agent.reload_model_config(
                    agent.config.provider, agent.config.model,
                    agent.config.base_url, agent.config.api_key_env)
            except Exception:
                logger.debug("registry.model_reload_error", exc_info=True)
        if personality_text is not None:
            from config import reverse_agent_name_replacements
            personality_text = reverse_agent_name_replacements(personality_text)
            # 解析人格文件路径：优先用已有路径，否则创建新路径
            existing_pf = _resolve_personality_path(agent.config.personality_file) \
                if agent.config.personality_file else None
            pf = Path(existing_pf) if existing_pf else self._personality_file(name)
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(personality_text, encoding="utf-8-sig")
            agent.config.personality_file = str(pf)
            await agent.init()  # 重载人格
        self._save_config(agent.config)
        return self._serialize(agent.config, enabled=name not in self._disabled)

    async def delete(self, name: str) -> None:
        """删除自定义 Agent，从 dispatcher 注销并清理配置文件。"""
        if name in BUILTIN_AGENTS or name == "xiaoda":
            raise ValueError("内置 Agent 不可删除，只能禁用")
        self._require(name)
        self.core.dispatcher.unregister(name)
        self._file(name).unlink(missing_ok=True)
        self._personality_file(name).unlink(missing_ok=True)

    def set_enabled(self, name: str, enabled: bool) -> None:
        """启用或禁用指定 Agent 并持久化状态。"""
        agent = self._require(name)
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        # 持久化禁用状态
        data = {}
        fp = self._file(name)
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
        else:
            for f in _CONFIG_FIELDS:
                v = getattr(agent.config, f, None)
                data[f] = sorted(v) if isinstance(v, set) else v
        data["_disabled"] = not enabled
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_enabled(self, name: str) -> bool:
        """返回指定 Agent 是否处于启用状态。"""
        return name not in self._disabled

    def _require(self, name: str) -> Any:
        """获取指定 Agent，不存在时抛出 KeyError。"""
        agent = self.core.dispatcher.get_agent(name)
        if not agent:
            raise KeyError(f"Agent {name} 不存在")
        return agent

    # ── 权限矩阵（R7）────────────────────────────────────

    @staticmethod
    def _blocked() -> set[str]:
        """返回子代理禁止使用的工具名称集合。"""
        from agent_dispatcher import DELEGATE_BLOCKED_TOOLS
        return set(DELEGATE_BLOCKED_TOOLS)

    @staticmethod
    def _all_tool_names() -> list[str]:
        """返回所有已注册工具的名称列表。"""
        from tool_engine.tool_registry import list_tools
        return [t["name"] for t in list_tools()]

    def get_permissions(self, name: str) -> dict:
        """获取指定 Agent 的工具和 MCP Server 权限矩阵。"""
        from tool_engine.tool_registry import list_tools
        blocked = self._blocked()
        if name == "xiaoda":
            cfg = self._load_xiaoda_cfg()
            excluded = set(cfg.get("excluded_tools") or [])
            mcp_allowed = list(cfg.get("mcp_servers") or [])
        else:
            agent = self._require(name)
            excluded = set(agent.config.excluded_tools or set())
            mcp_allowed = list(agent.config.mcp_servers or [])
        tools = {}
        for t in list_tools():
            n = t["name"]
            if n in blocked:
                tools[n] = {"enabled": False, "locked": True,
                            "reason": "系统锁定（防递归委托/长耗时）"}
            else:
                tools[n] = {"enabled": n not in excluded, "locked": False}
        mcp = {}
        try:
            mcp_names = list(self.core._mcp_manager._clients.keys())
        except Exception:
            logger.debug("registry.mcp_clients_error", exc_info=True)
            mcp_names = []
        for s in mcp_names:
            mcp[s] = {"enabled": s in mcp_allowed if mcp_allowed else True, "locked": False}
        return {"tools": tools, "mcp_servers": mcp, "is_main": False}

    def set_permissions(self, name: str, matrix: dict) -> dict:
        """设置指定 Agent 的工具和 MCP Server 权限并持久化。"""
        blocked = self._blocked()
        tools = matrix.get("tools") or {}
        if name == "xiaoda":
            cfg = self._load_xiaoda_cfg()
            excluded = set(cfg.get("excluded_tools") or [])
            for tool_name, enabled in tools.items():
                if tool_name in blocked:
                    continue
                if enabled:
                    excluded.discard(tool_name)
                else:
                    excluded.add(tool_name)
            cfg["excluded_tools"] = sorted(excluded)
            mcp = matrix.get("mcp_servers")
            if mcp is not None:
                cfg["mcp_servers"] = [s for s, on in mcp.items() if on]
            self._save_xiaoda_cfg(cfg)
            return self.get_permissions(name)
        agent = self._require(name)
        excluded = set(agent.config.excluded_tools or set())
        for tool_name, enabled in tools.items():
            if tool_name in blocked:
                continue  # 锁定项忽略
            if enabled:
                excluded.discard(tool_name)
            else:
                excluded.add(tool_name)
        agent.config.excluded_tools = excluded
        mcp = matrix.get("mcp_servers")
        if mcp is not None:
            agent.config.mcp_servers = [s for s, on in mcp.items() if on]
        self._save_config(agent.config)
        return self.get_permissions(name)

    # ── 人格 ────────────────────────────────────────────

    def get_personality(self, name: str) -> str:
        """获取指定 Agent 的人格文本内容（已应用 display_name 替换）。"""
        from config import apply_agent_name_replacements
        if name == "xiaoda":
            from config import WORKSPACE_DIR
            soul_path = WORKSPACE_DIR / "SOUL.md"
            if soul_path.exists():
                raw = soul_path.read_text(encoding="utf-8-sig")
                return apply_agent_name_replacements(raw)
            return ""
        agent = self._require(name)
        pf = _resolve_personality_path(agent.config.personality_file) \
            if agent.config.personality_file else None
        if pf:
            raw = Path(pf).read_text(encoding="utf-8-sig")
            return apply_agent_name_replacements(raw)
        return ""

    async def set_personality(self, name: str, text: str) -> None:
        """设置 Agent 的人格文本，写入文件并重新初始化。保存时还原 display_name 为原名。"""
        from config import reverse_agent_name_replacements
        text = reverse_agent_name_replacements(text)
        if name == "xiaoda":
            from config import WORKSPACE_DIR
            soul_path = WORKSPACE_DIR / "SOUL.md"
            soul_path.write_text(text, encoding="utf-8")
            return
        agent = self._require(name)
        existing_pf = _resolve_personality_path(agent.config.personality_file) \
            if agent.config.personality_file else None
        pf = Path(existing_pf) if existing_pf else self._personality_file(name)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(text, encoding="utf-8")
        agent.config.personality_file = str(pf)
        await agent.init()
        self._save_config(agent.config)

    # ── 模型一键切换 ────────────────────────────────────

    # 已知 provider → (api_key_env, base_url)
    # base_url 优先读环境变量覆盖，便于私有化部署
    _KNOWN_PROVIDERS: dict[str, tuple[str, str]] = {
        "mimo": ("MIMO_API_KEY", "https://api.xiaomimimo.com/v1"),
        "siliconflow": ("SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1"),
        "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        "modelscope": ("MODELSCOPE_ACCESS_TOKEN", "https://api-inference.modelscope.cn/v1"),
        "agnes": ("AGNES_API_KEY", "https://apihub.agnes-ai.com/v1"),
    }

    @classmethod
    def _resolve_provider_info(cls, provider: str) -> tuple[str, str]:
        """解析 provider 的 (base_url, api_key_env)。

        已知 provider 走标准 env var；自定义 provider 从 config_service 读取 base_url，
        并把文件中的 key 注入到 os.environ，使 SubAgent._read_env_key 能取到。
        """
        import os

        if provider in cls._KNOWN_PROVIDERS:
            api_key_env, default_base_url = cls._KNOWN_PROVIDERS[provider]
            # 允许环境变量覆盖 base_url（私有化部署）
            base_url = os.getenv(f"{provider.upper()}_BASE_URL", default_base_url)
            if provider == "mimo":
                base_url = os.getenv("MIMO_BASE_URL", default_base_url)
            elif provider == "agnes":
                base_url = os.getenv("AGNES_BASE_URL", default_base_url)
            return base_url, api_key_env

        # 自定义 provider → 从 config_service 读取
        from web.config_service import get_config_service
        from web.routers.models import load_provider_key
        cfg = get_config_service()
        record = cfg.get(f"models.providers.{provider}")
        if not record:
            raise ValueError(f"未知 provider: {provider}")
        base_url = record.get("base_url", "")
        if not base_url:
            raise ValueError(f"Provider {provider} 缺少 base_url")

        # 自定义 provider 的 key 存在文件中，注入到 os.environ 让 SubAgent 能读到
        key = load_provider_key(provider)
        if not key:
            raise ValueError(f"Provider {provider} 的 API Key 未配置")
        env_name = f"PROVIDER_{provider.upper().replace('-', '_')}_KEY"
        os.environ[env_name] = key
        return base_url, env_name

    async def set_agent_model(self, name: str, provider: str, model_id: str) -> dict:
        """一键切换子 Agent 的模型：自动解析 base_url 和 api_key_env，热重载并持久化。

        - 拒绝修改主体小妲
        - provider/model_id 不能为空
        - provider 必须在已知列表或自定义 provider 配置中
        """
        if name == "xiaoda":
            raise ValueError("主体小妲的模型不可通过此接口修改")
        agent = self._require(name)  # raises KeyError if not found

        provider = (provider or "").strip()
        model_id = (model_id or "").strip()
        if not provider or not model_id:
            raise ValueError("provider 和 model_id 不能为空")

        # 解析 provider 信息（可能抛 ValueError）
        base_url, api_key_env = self._resolve_provider_info(provider)

        # 热重载：创建新 client 并原子替换，不重新探活
        ok = await agent.reload_model_config(provider, model_id, base_url, api_key_env)
        if not ok:
            raise ValueError(f"模型热重载失败（检查 {api_key_env} 是否配置）")

        # 持久化到 config/agents/{name}.json
        self._save_config(agent.config)
        logger.info("agent_registry.model_set name={} provider={} model={}",
                    name, provider, model_id)
        return self._serialize(agent.config, enabled=name not in self._disabled)
