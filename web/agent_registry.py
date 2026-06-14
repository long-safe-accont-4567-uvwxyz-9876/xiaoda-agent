"""AgentRegistry — Agent 的 CRUD、持久化与权限矩阵（R5/R6/R7）。

内置 4 个子代理由 core/bootstrap.py 注册；本模块负责：
- 加载/保存 config/agents/*.json 中的用户自建 Agent 与对内置 Agent 的覆盖
- 运行时热插拔（dispatcher.register / unregister）
- 权限矩阵读写（excluded_tools / mcp_servers，改完即时生效，
  因为 SubAgent._filtered_tools() 每次对话时实时计算）
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from loguru import logger

AGENTS_DIR = Path(__file__).resolve().parent.parent / "config" / "agents"
BUILTIN_AGENTS = {"keli", "yinlang", "xilian", "nike"}
# 内置 Agent 默认背景板（打包在前端 dist/assets/wallpapers/ 下）
DEFAULT_WALLPAPERS = {
    "nahida": "/assets/webui_background.jpg",
    "keli": "/assets/wallpapers/keli.jpg",
    "yinlang": "/assets/wallpapers/yinlang.jpg",
    "xilian": "/assets/wallpapers/xilian.jpg",
    "nike": "/assets/wallpapers/nike.jpg",
}
# 主体纳西妲不是 SubAgent，但要出现在 Agent 列表里供切换
MAIN_AGENT_META = {
    "name": "nahida",
    "display_name": "纳西妲",
    "builtin": True,
    "is_main": True,
    "enabled": True,
    "provider": "mimo",
    "wallpaper": DEFAULT_WALLPAPERS["nahida"],
    "route_description": "主体，默认对话对象，可委托其他子代理",
}

# 配置文件中允许的字段（与 SubAgentConfig 对齐）
_CONFIG_FIELDS = [
    "name", "display_name", "provider", "model", "personality_file", "voice_ref",
    "excluded_tools", "base_url", "api_key_env", "capabilities", "route_description",
    "mcp_servers", "max_spawn_depth", "max_turns", "effort", "permission_mode",
    "memory_scope", "background", "wallpaper",
]


class AgentRegistry:
    def __init__(self, core):
        self.core = core
        self._disabled: set[str] = set()
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 持久化 ──────────────────────────────────────────

    def _file(self, name: str) -> Path:
        return AGENTS_DIR / f"{name}.json"

    def _personality_file(self, name: str) -> Path:
        return AGENTS_DIR / f"{name}_personality.md"

    def _save_config(self, cfg) -> None:
        data = {}
        for f in _CONFIG_FIELDS:
            v = getattr(cfg, f, None)
            if isinstance(v, set):
                v = sorted(v)
            data[f] = v
        data["_saved_at"] = time.time()
        self._file(cfg.name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def load_persisted(self) -> None:
        """启动时调用：恢复自建 Agent 并应用对内置 Agent 的覆盖。"""
        from agent_dispatcher import SubAgentConfig
        for fp in sorted(AGENTS_DIR.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                name = data.get("name", "")
                if not name:
                    continue
                if name in BUILTIN_AGENTS:
                    agent = self.core.dispatcher.get_agent(name)
                    if agent:
                        self._apply_fields(agent.config, data)
                        logger.info("agent_registry.builtin_override name={}", name)
                else:
                    kwargs = {f: data[f] for f in _CONFIG_FIELDS if f in data and data[f] is not None}
                    kwargs["excluded_tools"] = set(kwargs.get("excluded_tools") or [])
                    cfg = SubAgentConfig(**kwargs)
                    await self.core.dispatcher.register(cfg)
                    logger.info("agent_registry.custom_loaded name={}", name)
                if data.get("_disabled"):
                    self._disabled.add(name)
            except Exception as e:
                logger.warning("agent_registry.load_failed file={} error={}", fp.name, str(e))

    @staticmethod
    def _apply_fields(cfg, data: dict) -> None:
        for f in _CONFIG_FIELDS:
            if f in ("name",) or f not in data or data[f] is None:
                continue
            v = data[f]
            if f == "excluded_tools":
                v = set(v)
            setattr(cfg, f, v)

    # ── 查询 ────────────────────────────────────────────

    def list(self) -> list[dict]:
        main = dict(MAIN_AGENT_META,
                    model=self._main_model(),
                    tool_count=len(self._all_tool_names()),
                    mcp_servers=[])
        try:
            from web.config_service import get_config_service
            wp = get_config_service().get("ui.main_wallpaper")
            if wp:
                main["wallpaper"] = wp
        except Exception:
            pass
        out = [main]
        for info in self.core.dispatcher.list_agents():
            name = info.get("name", "")
            agent = self.core.dispatcher.get_agent(name)
            if not agent:
                continue
            cfg = agent.config
            out.append(self._serialize(cfg, enabled=name not in self._disabled))
        return out

    def _main_model(self) -> str:
        try:
            from model_router import ROUTE_TABLE
            return ROUTE_TABLE.get("chat", {}).get("model", "")
        except Exception:
            return ""

    def _serialize(self, cfg, enabled: bool = True) -> dict:
        excluded = set(cfg.excluded_tools or set())
        tool_count = len([t for t in self._all_tool_names()
                          if t not in excluded and t not in self._blocked()])
        return {
            "name": cfg.name,
            "display_name": cfg.display_name,
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
            "tool_count": tool_count,
        }

    def get(self, name: str) -> dict | None:
        if name == "nahida":
            return self.list()[0]
        agent = self.core.dispatcher.get_agent(name)
        if not agent:
            return None
        return self._serialize(agent.config, enabled=name not in self._disabled)

    # ── 增删改 ──────────────────────────────────────────

    async def create(self, data: dict) -> dict:
        from agent_dispatcher import SubAgentConfig
        name = (data.get("name") or "").strip().lower()
        if not name or not name.isidentifier():
            raise ValueError("name 必须是合法标识符（小写字母/数字/下划线）")
        if name == "nahida" or self.core.dispatcher.get_agent(name):
            raise ValueError(f"Agent {name} 已存在")
        personality_text = data.pop("personality_text", "")
        kwargs = {f: data[f] for f in _CONFIG_FIELDS if f in data and data[f] is not None}
        kwargs["name"] = name
        kwargs["excluded_tools"] = set(kwargs.get("excluded_tools") or [])
        if personality_text:
            pf = self._personality_file(name)
            pf.write_text(personality_text, encoding="utf-8")
            kwargs["personality_file"] = str(pf)
        cfg = SubAgentConfig(**kwargs)
        ok = await self.core.dispatcher.register(cfg)
        if not ok:
            raise ValueError("dispatcher 注册失败（检查 provider/api_key_env）")
        self._save_config(cfg)
        return self._serialize(cfg)

    async def update(self, name: str, data: dict) -> dict:
        agent = self._require(name)
        personality_text = data.pop("personality_text", None)
        self._apply_fields(agent.config, data)
        if personality_text is not None:
            pf = Path(agent.config.personality_file) if agent.config.personality_file \
                else self._personality_file(name)
            pf.write_text(personality_text, encoding="utf-8")
            agent.config.personality_file = str(pf)
            await agent.init()  # 重载人格
        self._save_config(agent.config)
        return self._serialize(agent.config, enabled=name not in self._disabled)

    async def delete(self, name: str) -> None:
        if name in BUILTIN_AGENTS or name == "nahida":
            raise ValueError("内置 Agent 不可删除，只能禁用")
        self._require(name)
        self.core.dispatcher.unregister(name)
        self._file(name).unlink(missing_ok=True)
        self._personality_file(name).unlink(missing_ok=True)

    def set_enabled(self, name: str, enabled: bool) -> None:
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
        return name not in self._disabled

    def _require(self, name: str):
        agent = self.core.dispatcher.get_agent(name)
        if not agent:
            raise KeyError(f"Agent {name} 不存在")
        return agent

    # ── 权限矩阵（R7）────────────────────────────────────

    @staticmethod
    def _blocked() -> set[str]:
        from agent_dispatcher import DELEGATE_BLOCKED_TOOLS
        return set(DELEGATE_BLOCKED_TOOLS)

    @staticmethod
    def _all_tool_names() -> list[str]:
        from tool_engine.tool_registry import list_tools
        return [t["name"] for t in list_tools()]

    def get_permissions(self, name: str) -> dict:
        from tool_engine.tool_registry import list_tools
        blocked = self._blocked()
        if name == "nahida":
            # 主体不经 dispatcher 过滤，所有工具可用（受全局开关控制）
            excluded: set[str] = set()
            mcp_allowed: list[str] = []
            is_main = True
        else:
            agent = self._require(name)
            excluded = set(agent.config.excluded_tools or set())
            mcp_allowed = list(agent.config.mcp_servers or [])
            is_main = False
        tools = {}
        for t in list_tools():
            n = t["name"]
            if not is_main and n in blocked:
                tools[n] = {"enabled": False, "locked": True,
                            "reason": "系统锁定（防递归委托/长耗时）"}
            else:
                tools[n] = {"enabled": n not in excluded, "locked": is_main,
                            "reason": "主体始终拥有全部工具" if is_main else ""}
        mcp = {}
        try:
            mcp_names = list(self.core._mcp_manager._clients.keys())
        except Exception:
            mcp_names = []
        for s in mcp_names:
            mcp[s] = {"enabled": is_main or s in mcp_allowed, "locked": is_main}
        return {"tools": tools, "mcp_servers": mcp, "is_main": is_main}

    def set_permissions(self, name: str, matrix: dict) -> dict:
        if name == "nahida":
            raise ValueError("主体纳西妲的工具不可裁剪")
        agent = self._require(name)
        blocked = self._blocked()
        tools = matrix.get("tools") or {}
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
        agent = self._require(name)
        pf = agent.config.personality_file
        if pf and Path(pf).exists():
            return Path(pf).read_text(encoding="utf-8")
        return ""

    async def set_personality(self, name: str, text: str) -> None:
        agent = self._require(name)
        pf = Path(agent.config.personality_file) if agent.config.personality_file \
            else self._personality_file(name)
        pf.write_text(text, encoding="utf-8")
        agent.config.personality_file = str(pf)
        await agent.init()
        self._save_config(agent.config)
