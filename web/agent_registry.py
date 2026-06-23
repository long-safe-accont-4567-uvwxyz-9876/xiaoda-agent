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

# 内置 Agent 的 excluded_tools（与 core/bootstrap.py 中 _register_sub_agents 保持一致）
# 用于降级模式下 _builtin_stub() 计算实际 tool_count
BUILTIN_EXCLUDED_TOOLS: dict[str, set[str]] = {
    "keli": {"call_klee", "shell_command", "python_executor", "write_file",
             "search_files", "read_file", "list_files", "web_browse",
             "document_reader", "multi_search", "wolfram_query"},
    "yinlang": {"call_klee", "call_nahida"},
    "xilian": {"call_klee", "call_nahida", "shell_command", "python_executor", "write_file"},
    "nike": {"call_klee", "call_nahida", "shell_command", "write_file"},
}
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

    # ── 内置 Agent 桩数据（降级模式下 dispatcher 未注册时使用）──

    _BUILTIN_STUBS: dict[str, dict] = {
        "keli": {
            "display_name": "可莉", "provider": "mimo", "model": "mimo-v2.5-pro",
            "route_description": "日常聊天、玩耍、轻松有趣的对话",
            "capabilities": ["chat", "play", "fun"],
        },
        "yinlang": {
            "display_name": "银狼", "provider": "mimo", "model": "mimo-v2.5-pro",
            "route_description": "编程、代码编写、调试、技术问题、硬件控制、系统运维、开发辅助",
            "capabilities": ["coding", "debug", "script", "programming", "hardware", "system", "devops"],
        },
        "xilian": {
            "display_name": "昔涟", "provider": "mimo", "model": "mimo-v2.5-pro",
            "route_description": "搜索信息、查询资料、探索发现",
            "capabilities": ["search", "lookup", "query", "explore", "discover"],
        },
        "nike": {
            "display_name": "尼可", "provider": "mimo", "model": "mimo-v2.5-pro",
            "route_description": "研究分析、学术思考、深度解读",
            "capabilities": ["research", "analysis", "study", "academic"],
        },
    }

    def _builtin_stub(self, name: str) -> dict:
        stub = self._BUILTIN_STUBS.get(name, {})
        excluded = BUILTIN_EXCLUDED_TOOLS.get(name, set())
        blocked = self._blocked()
        # 实际 tool_count = 全部工具 - 该 agent 排除的工具 - 子代理禁用工具
        tool_count = len([t for t in self._all_tool_names()
                          if t not in excluded and t not in blocked])
        return {
            "name": name,
            "display_name": stub.get("display_name", name),
            "builtin": True,
            "is_main": False,
            "enabled": True,
            "provider": stub.get("provider", ""),
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
            "tool_count": tool_count,
            "degraded": True,
        }

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
        try:
            from model_router import ROUTE_TABLE
            return ROUTE_TABLE.get("chat", {}).get("model", "")
        except Exception:
            return ""

    def _serialize(self, cfg, enabled: bool = True, degraded: bool = False) -> dict:
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
            "degraded": degraded,
        }

    def get(self, name: str) -> dict | None:
        if name == "nahida":
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
        # 主体 nahida 特殊处理：不在 dispatcher 中，只更新壁纸/人格
        if name == "nahida":
            if data.get("wallpaper"):
                from web.config_service import get_config_service
                get_config_service().set("ui.main_wallpaper", data["wallpaper"])
            personality_text = data.pop("personality_text", None)
            if personality_text is not None:
                from config import WORKSPACE_DIR
                soul_path = WORKSPACE_DIR / "SOUL.md"
                soul_path.write_text(personality_text, encoding="utf-8")
            return self.get("nahida")
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
                pass  # 未知 provider 时不强制覆盖
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
                pass  # 热重载失败不阻断保存，下次启动时会重试
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
        if name == "nahida":
            from config import WORKSPACE_DIR
            soul_path = WORKSPACE_DIR / "SOUL.md"
            if soul_path.exists():
                return soul_path.read_text(encoding="utf-8")
            return ""
        agent = self._require(name)
        pf = agent.config.personality_file
        if pf and Path(pf).exists():
            return Path(pf).read_text(encoding="utf-8")
        return ""

    async def set_personality(self, name: str, text: str) -> None:
        if name == "nahida":
            from config import WORKSPACE_DIR
            soul_path = WORKSPACE_DIR / "SOUL.md"
            soul_path.write_text(text, encoding="utf-8")
            return
        agent = self._require(name)
        pf = Path(agent.config.personality_file) if agent.config.personality_file \
            else self._personality_file(name)
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

        - 拒绝修改主体 nahida
        - provider/model_id 不能为空
        - provider 必须在已知列表或自定义 provider 配置中
        """
        if name == "nahida":
            raise ValueError("主体纳西妲的模型不可通过此接口修改")
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
