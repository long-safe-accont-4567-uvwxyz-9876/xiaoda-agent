"""插件 API 桥接层 — 桥接插件与宿主子系统"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

from loguru import logger

import config
from hooks import BaseHook, HookResult, HookType
from plugins.manifest import PluginManifest
from plugins.permissions import PermissionChecker


class _PluginHook(BaseHook):
    """将 SDK 事件处理器适配为宿主 HookEngine 的 BaseHook。"""

    def __init__(self, plugin_id: str, event_type: str, handler: Callable) -> None:
        try:
            self.hook_type = HookType(event_type)
        except ValueError as exc:
            raise ValueError(f"Unsupported plugin event type: {event_type!r}") from exc
        self.name = f"plugin:{plugin_id}:{event_type}:{handler.__name__}"
        self._handler = handler

    async def execute(self, context: dict) -> HookResult:
        result = self._handler(context)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return HookResult()
        if isinstance(result, HookResult):
            return result
        raise TypeError(
            f"Plugin hook {self.name} must return HookResult or None, "
            f"got {type(result).__name__}",
        )


class PluginContext:
    """插件与宿主之间的桥接层"""

    def __init__(self, manifest: PluginManifest, permissions: PermissionChecker,
                 tool_registry: Any | None=None, hook_engine: Any | None=None, memory_manager: Any | None=None,
                 knowledge_graph: Any | None=None, mcp_manager: Any | None=None, agent_core: Any | None=None) -> None:
        self._manifest = manifest
        self._permissions = permissions
        self._tool_registry = tool_registry
        self._hook_engine = hook_engine
        self._memory = memory_manager
        self._kg = knowledge_graph
        self._mcp = mcp_manager
        self._agent_core = agent_core
        self._plugin_id = manifest.id
        self._tool_declarations: dict[str, dict[str, Any]] = {}
        self._hook_declarations: list[tuple[str, Callable]] = []
        self._registrations_active = False
        self._registered_tools: set[str] = set()
        self._registered_hooks: dict[tuple[str, Callable], _PluginHook] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._data_dir = config.PLUGINS_CONFIG_DIR / manifest.id / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    # ── Tool Registration ──
    def register_tool(self, name: str, handler: Callable, description: str = "",
                      schema: dict | None = None, category: str = "plugin") -> None:
        """声明工具；仅在插件启用后发布到全局注册表。"""
        full_name = f"{self._plugin_id}__{name}" if "__" not in name else name
        self._tool_declarations[full_name] = {
            "name": full_name,
            "handler": handler,
            "description": description,
            "schema": schema,
            "category": category,
        }
        if self._registrations_active:
            self._activate_tool(self._tool_declarations[full_name])

    def _activate_tool(self, declaration: dict[str, Any]) -> None:
        if self._tool_registry is None:
            logger.warning("plugin.tool_registry_unavailable", plugin=self._plugin_id)
            return
        from tool_engine.tool_registry import ToolPermission, register_tool_direct
        full_name = declaration["name"]
        register_tool_direct(
            name=full_name,
            description=declaration["description"],
            func=declaration["handler"],
            parameters=declaration["schema"] or {"type": "object", "properties": {}},
            permission=ToolPermission.EXECUTE,
            category=declaration["category"],
            source=f"plugin:{self._plugin_id}",
            plugin_id=self._plugin_id,
            version=self._manifest.version,
        )
        self._registered_tools.add(full_name)
        logger.info("plugin.tool_registered", plugin=self._plugin_id, tool=full_name)

    def unregister_tool(self, name: str) -> None:
        """取消注册工具"""
        if self._tool_registry is not None:
            from tool_engine.tool_registry import unregister_tool
            try:
                unregister_tool(name)
                self._registered_tools.discard(name)
            except Exception as e:
                logger.debug("plugin.tool_unregister_failed", tool=name, error=str(e))

    # ── Event Subscription ──
    def subscribe(self, event_type: str, handler: Callable) -> None:
        """声明事件订阅；仅在插件启用后发布到 hook engine。"""
        declaration = (event_type, handler)
        if declaration not in self._hook_declarations:
            self._hook_declarations.append(declaration)
        if self._registrations_active:
            self._activate_hook(event_type, handler)

    def _activate_hook(self, event_type: str, handler: Callable) -> None:
        if self._hook_engine is None:
            return
        declaration = (event_type, handler)
        if declaration in self._registered_hooks:
            return
        hook = _PluginHook(self._plugin_id, event_type, handler)
        self._hook_engine.register(hook)
        self._registered_hooks[declaration] = hook
        logger.info("plugin.subscribed", plugin=self._plugin_id, event=event_type)

    def unsubscribe(self, event_type: str, handler: Callable | None = None) -> None:
        """取消一个处理器或该事件类型下的全部插件订阅。"""
        if self._hook_engine is None:
            return
        for declaration, hook in list(self._registered_hooks.items()):
            declared_event, declared_handler = declaration
            if declared_event != event_type:
                continue
            if handler is not None and declared_handler != handler:
                continue
            self._hook_engine.unregister(hook)
            self._registered_hooks.pop(declaration, None)

    # ── Plugin Data ──
    def plugin_data_get(self, key: str, default: Any = None) -> Any:
        """读取插件私有数据"""
        self._permissions.check_plugin_data_read()
        data_file = self._data_dir / "data.json"
        if not data_file.exists():
            return default
        try:
            with open(data_file, encoding="utf-8") as f:
                data = json.load(f)
            return data.get(key, default)
        except (ValueError, KeyError, ImportError):
            return default

        except Exception:
            logger.exception(".plugins.context.plugin_data_get_unexpected")
            return default

    def plugin_data_set(self, key: str, value: Any) -> None:
        """写入插件私有数据"""
        self._permissions.check_plugin_data_write()
        data_file = self._data_dir / "data.json"
        data = {}
        if data_file.exists():
            try:
                with open(data_file, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.debug("plugin_context.data_load_failed", exc_info=True)
        data[key] = value
        with open(data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def plugin_data_delete(self, key: str) -> None:
        """删除插件私有数据"""
        self._permissions.check_plugin_data_write()
        data_file = self._data_dir / "data.json"
        if not data_file.exists():
            return
        try:
            with open(data_file, encoding="utf-8") as f:
                data = json.load(f)
            data.pop(key, None)
            with open(data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("plugin_context.data_delete_failed", exc_info=True)

    # ── Memory ──
    async def memory_search(self, query: str, k: int = 5) -> list[dict]:
        """搜索记忆"""
        self._permissions.check_memory_read()
        if self._memory is None:
            return []
        return await self._memory.retrieve_memories(query, k=k)

    async def memory_store(self, summary: str, importance: float = 0.5) -> int | None:
        """存储记忆"""
        self._permissions.check_memory_write()
        if self._memory is None:
            return None
        try:
            return await self._memory.memory.insert_episodic_memory(
                summary=summary, importance=importance, session_id=f"plugin:{self._plugin_id}"
            )
        except Exception as e:
            logger.warning("plugin.memory_store_failed", plugin=self._plugin_id, error=str(e))
            return None

    # ── LLM ──
    async def llm_chat(self, messages: list[dict], **kwargs: Any) -> str:
        """调用 LLM"""
        self._permissions.check_llm_access()
        if self._agent_core is None:
            return ""
        try:
            return await self._agent_core.router.chat(messages=messages, **kwargs)
        except Exception as e:
            logger.warning("plugin.llm_chat_failed", plugin=self._plugin_id, error=str(e))
            return ""

    # ── Background Tasks ──
    def spawn_task(self, name: str, coro: Any) -> None:
        """启动后台任务（同名防覆盖：旧任务未结束则先取消并等待其退出）。

        旧行为直接覆盖 dict 引用，旧任务仍在跑且永远失去取消入口——
        插件热更新重连场景会造成同一协程双实例并行（副作用重复）。
        """
        old = self._background_tasks.get(name)
        if old is not None and not old.done():
            old.cancel()
            logger.warning("plugin.task_superseded", plugin=self._plugin_id,
                           task=name)
        task = asyncio.create_task(coro, name=f"plugin:{self._plugin_id}:{name}")

        def _done(done: asyncio.Task) -> None:
            # 仅在"自己仍是登记实例"时清位，避免误删后来者
            if self._background_tasks.get(name) is done:
                self._background_tasks.pop(name, None)
            if not done.cancelled() and done.exception() is not None:
                logger.warning("plugin.task_failed", plugin=self._plugin_id,
                               task=name, error=str(done.exception())[:200])

        task.add_done_callback(_done)
        self._background_tasks[name] = task
        logger.info("plugin.task_spawned", plugin=self._plugin_id, task=name)

    def cancel_task(self, name: str) -> None:
        """取消后台任务"""
        task = self._background_tasks.pop(name, None)
        if task and not task.done():
            task.cancel()

    def cancel_all_tasks(self) -> None:
        """取消所有后台任务"""
        for _name, task in list(self._background_tasks.items()):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

    # ── Cleanup ──
    def activate_registrations(self) -> None:
        """发布当前声明；失败由管理器调用 clear_registrations 回滚。"""
        self._registrations_active = True
        for declaration in self._tool_declarations.values():
            self._activate_tool(declaration)
        for event_type, handler in self._hook_declarations:
            self._activate_hook(event_type, handler)

    def clear_registrations(self) -> None:
        """清除已发布注册，保留声明供后续重新启用。"""
        self._registrations_active = False
        for tool_name in self._registered_tools:
            try:
                from tool_engine.tool_registry import unregister_tool
                unregister_tool(tool_name)
            except Exception:
                logger.debug("plugin.unregister_tool_failed", exc_info=True)
        self._registered_tools.clear()
        for event_type, handler in list(self._registered_hooks):
            try:
                self.unsubscribe(event_type, handler)
            except Exception:
                logger.debug("plugin.unsubscribe_failed", exc_info=True)
        self._registered_hooks.clear()
        self.cancel_all_tasks()
