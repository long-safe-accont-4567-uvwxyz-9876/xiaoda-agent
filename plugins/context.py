"""插件 API 桥接层 — 桥接插件与宿主子系统"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from plugins.manifest import PluginManifest
from plugins.permissions import PermissionChecker


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
        self._registered_tools: list[str] = []
        self._registered_hooks: list[tuple[str, Callable]] = []
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._data_dir = Path(f"config/plugins/{manifest.id}")
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
        """注册工具到全局 tool_registry"""
        if self._tool_registry is None:
            logger.warning("plugin.tool_registry_unavailable", plugin=self._plugin_id)
            return
        full_name = f"{self._plugin_id}__{name}" if "__" not in name else name
        from tool_engine.tool_registry import register_tool_direct, ToolPermission
        register_tool_direct(
            name=full_name,
            description=description,
            func=handler,
            parameters=schema or {"type": "object", "properties": {}},
            permission=ToolPermission.EXECUTE,
            category=category,
            source=f"plugin:{self._plugin_id}",
            plugin_id=self._plugin_id,
            version=self._manifest.version,
        )
        self._registered_tools.append(full_name)
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
        """订阅事件"""
        if self._hook_engine is not None:
            self._hook_engine.register(event_type, handler, plugin_id=self._plugin_id)
            self._registered_hooks.append((event_type, handler))
            logger.info("plugin.subscribed", plugin=self._plugin_id, event=event_type)

    def unsubscribe(self, event_type: str, handler: Callable | None = None) -> None:
        """取消订阅事件"""
        if self._hook_engine is not None:
            self._hook_engine.unregister(event_type, handler, plugin_id=self._plugin_id)

    # ── Plugin Data ──
    def plugin_data_get(self, key: str, default: Any = None) -> Any:
        """读取插件私有数据"""
        self._permissions.check_plugin_data_read()
        data_file = self._data_dir / "data.json"
        if not data_file.exists():
            return default
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(key, default)
        except Exception:
            return default

    def plugin_data_set(self, key: str, value: Any) -> None:
        """写入插件私有数据"""
        self._permissions.check_plugin_data_write()
        data_file = self._data_dir / "data.json"
        data = {}
        if data_file.exists():
            try:
                with open(data_file, "r", encoding="utf-8") as f:
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
            with open(data_file, "r", encoding="utf-8") as f:
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
        """启动后台任务"""
        task = asyncio.create_task(coro)
        self._background_tasks[name] = task
        logger.info("plugin.task_spawned", plugin=self._plugin_id, task=name)

    def cancel_task(self, name: str) -> None:
        """取消后台任务"""
        task = self._background_tasks.pop(name, None)
        if task and not task.done():
            task.cancel()

    def cancel_all_tasks(self) -> None:
        """取消所有后台任务"""
        for name, task in list(self._background_tasks.items()):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

    # ── Cleanup ──
    def clear_registrations(self) -> None:
        """清除所有注册（工具、事件）"""
        for tool_name in self._registered_tools:
            try:
                from tool_engine.tool_registry import unregister_tool
                unregister_tool(tool_name)
            except Exception:
                logger.debug("plugin.unregister_tool_failed", exc_info=True)
        self._registered_tools.clear()
        for event_type, handler in self._registered_hooks:
            try:
                self.unsubscribe(event_type, handler)
            except Exception:
                logger.debug("plugin.unsubscribe_failed", exc_info=True)
        self._registered_hooks.clear()
        self.cancel_all_tasks()