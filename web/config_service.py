"""ConfigService — Web UI 可改配置的统一存取层（R8/R22）。

所有 UI 修改的配置写入 config/webui_overrides.json（原子写盘），
并立即触发注册的回调使内存对象热生效。密钥永不进入此文件。
"""
from __future__ import annotations

import json
import threading
import traceback
from pathlib import Path
from typing import Any
from collections.abc import Callable

from loguru import logger


# ── 调试: TrackedDict 捕获所有直接变异 ──────────────────────────
# 历史背景: 配置文件在运行时被神秘覆盖为 mimo，根因调查阶段用 TrackedDict
# 捕获通过 cfg.get() 引用直接变异 _data 的代码路径（Python 陷阱: get() 返回引用）。
# 根因已定位并通过 get() 深拷贝修复，_TRACK_MUTATIONS 设为 False 关闭追踪。
# 保留 TrackedDict 类作为可选诊断工具，需要时设为 True 即可重新启用。
_TRACK_MUTATIONS = False  # 根因已定位并修复，关闭变异追踪避免生产日志污染


class _TrackedDict(dict):
    """调试用: 追踪所有变异操作的 dict 子类。

    当 _TRACK_MUTATIONS=True 时，所有写入操作都会记录 key、value 摘要和调用堆栈，
    用于定位直接变异 _data 的代码路径（绕过 ConfigService.set() 的污染）。
    """

    __slots__ = ("_track_path",)

    def __init__(self, *args, _track_path: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_track_path", _track_path)

    @staticmethod
    def _should_log() -> bool:
        return _TRACK_MUTATIONS

    def _log_mutation(self, op: str, key: Any, value: Any = None) -> None:
        if not self._should_log():
            return
        try:
            val_str = str(value)[:120] if value is not None else ""
            stack = "".join(traceback.format_stack(limit=10)[:-1])
            logger.debug(
                "config_service.data_mutation_direct path={}.{} op={} key={} value={} stack=\n{}",
                self._track_path, op, str(key)[:50], val_str, stack,
            )
        except Exception:
            pass

    def __setitem__(self, key: Any, value: Any) -> None:
        if self._should_log() and isinstance(key, str):
            self._log_mutation("__setitem__", key, value)
        # 递归包装嵌套 dict 为 TrackedDict
        if isinstance(value, dict) and not isinstance(value, _TrackedDict):
            new_path = f"{self._track_path}.{key}" if self._track_path else str(key)
            value = _wrap_tracked(value, new_path)
        super().__setitem__(key, value)

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        if self._should_log():
            self._log_mutation("update", args or kwargs)
        # 通过 __setitem__ 逐项写入，避免原地修改调用方传入的 dict（副作用）
        # __setitem__ 会自动将嵌套 dict 包装为 _TrackedDict
        if args:
            for k, v in (args[0].items() if isinstance(args[0], dict) else args[0]):
                self.__setitem__(k, v)
        for k, v in kwargs.items():
            self.__setitem__(k, v)

    def pop(self, key: Any, *default: Any) -> Any:  # type: ignore[override]
        if self._should_log():
            self._log_mutation("pop", key)
        return super().pop(key, *default)

    def popitem(self) -> Any:  # type: ignore[override]
        if self._should_log():
            self._log_mutation("popitem", "")
        return super().popitem()

    def clear(self) -> None:  # type: ignore[override]
        if self._should_log():
            self._log_mutation("clear", "")
        super().clear()

    def __delitem__(self, key: Any) -> None:
        if self._should_log():
            self._log_mutation("__delitem__", key)
        super().__delitem__(key)


def _wrap_tracked(data: dict, path: str = "") -> _TrackedDict:
    """递归包装普通 dict 为 TrackedDict，保持路径追踪。"""
    result = _TrackedDict(_track_path=path)
    for k, v in data.items():
        child_path = f"{path}.{k}" if path else str(k)
        if isinstance(v, dict) and not isinstance(v, _TrackedDict):
            result[k] = _wrap_tracked(v, child_path)  # type: ignore[assignment]
        elif isinstance(v, list):
            result[k] = _wrap_list_items(v, child_path)  # type: ignore[assignment]
        else:
            result[k] = v  # type: ignore[assignment]
    return result


def _wrap_list_items(lst: list, path: str) -> list:
    """递归包装 list 中的 dict 元素。"""
    result = []
    for i, item in enumerate(lst):
        if isinstance(item, dict) and not isinstance(item, _TrackedDict):
            result.append(_wrap_tracked(item, f"{path}[{i}]"))
        elif isinstance(item, list):
            result.append(_wrap_list_items(item, f"{path}[{i}]"))
        else:
            result.append(item)
    return result


def _get_overrides_path() -> Path:
    from config import get_config_dir
    return get_config_dir() / "webui_overrides.json"

_DEFAULTS: dict[str, Any] = {
    "schedule": {
        "enabled": True,
        "greeting_max_per_day": 3,
        "dnd_periods": [{"start": "23:00", "end": "08:00"}],
    },
    "tts": {"auto_speak": False, "default_voice": "xiaoda"},
    "ui": {"particles": "medium", "tilt3d": True},
    "tools": {},      # {tool_name: {"enabled": false, "max_frequency": 5}}
    "mcp": {},        # {server_name: {command, args, env, agents, enabled}} 用户新增的
    "models": {"providers": {}, "routes": {}},
    "dashboard": {"system_monitor_enabled": False},
    # 可观测性: Prometheus /metrics 端点开关 (默认开启)
    # 同时受环境变量 METRICS_ENABLED 控制 (env 优先级高于 webui_overrides.json)
    "observability": {"metrics_enabled": True},
    "mail": {
        "enabled": False,
        "mode": "off",  # off / allowlist / all
        "allowed_senders": [],
        "reply_channel": "mail",  # mail / mail_and_qq
        "max_per_day": 50,
        "dnd_start": 0,  # 免打扰开始小时（0-23），0=不启用 DND
        "dnd_end": 0,    # 免打扰结束小时（0-23），与 dnd_start 相同=不启用
    },
}


class ConfigService:
    def __init__(self, path: Path | None = None) -> None:
        """初始化配置服务, 加载已存在的覆盖文件.

        Args:
            path: 覆盖配置文件路径, None 表示使用默认路径
        """
        self._path = path or _get_overrides_path()
        self._lock = threading.Lock()
        # 使用 TrackedDict 包装 _data，捕获所有直接变异操作
        self._data: dict[str, Any] = _wrap_tracked(json.loads(json.dumps(_DEFAULTS)), "root")
        self._watchers: dict[str, list[Callable[[Any], None]]] = {}
        self._startup_complete: bool = False  # 启动完成后启用 _save 一致性验证
        self._load()

    def mark_startup_complete(self) -> None:
        """标记启动完成，启用 _save() 一致性验证。

        在 _sync_current_chat_model 结束后由 server.py 调用。
        启动期间 _save() 不做一致性验证（ROUTE_TABLE 尚未从持久化恢复），
        避免误判：启动时 ROUTE_TABLE 默认为 mimo，但 _data 已从磁盘加载 agnes。
        """
        self._startup_complete = True
        logger.info("config_service.startup_complete validation_enabled=True")

    def _load(self) -> None:
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                # deep_merge 后重新包装为 TrackedDict，确保所有嵌套层都被追踪
                self._deep_merge(self._data, saved)
                # 重新包装以确保加载的数据也是 TrackedDict
                self._data = _wrap_tracked(dict(self._data), "root")
            except Exception as e:
                logger.warning("config_service.load_failed error={}", str(e))

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                ConfigService._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, path: str, default: Any = None) -> Any:
        """按点分路径读取配置值.

        Args:
            path: 点分路径 (如 "schedule.enabled")
            default: 路径不存在时的默认返回值

        Returns:
            配置值或 default

        根因修复: 对 models. 路径返回深拷贝，防止调用方通过引用直接变异 _data。
        Python 陷阱: dict 的 get/[] 返回内部对象的引用，直接修改返回值会污染 _data
        而不触发 set()/_save()。这是模型配置被神秘覆盖为 mimo 的根因：
        某代码通过 cfg.get("models.routes") 获取引用后直接修改，
        随后非 models 路径的 set() 触发 _save() 将污染的 _data 持久化。
        深拷贝切断引用链，使调用方的修改不影响 _data。
        """
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        # 防御性深拷贝: models. 路径返回深拷贝，防止引用变异污染 _data
        if path.startswith("models.") and isinstance(node, (dict, list)):
            return json.loads(json.dumps(node))
        return node

    def set(self, path: str, value: Any) -> None:
        """按点分路径设置配置项, 落盘并通知 watcher.

        Args:
            path: 点分路径
            value: 新值
        """
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                # setdefault 创建的 dict 也必须是 TrackedDict
                if part not in node or not isinstance(node[part], dict):
                    child_path = f"{getattr(node, '_track_path', '')}.{part}"
                    node[part] = _TrackedDict(_track_path=child_path)
                node = node[part]
            node[parts[-1]] = value
            self._save()
        # 审计日志：models. 路径写入记录简洁 INFO（无堆栈），便于追踪模型配置变更
        if path.startswith("models."):
            logger.info("config_service.models_write path={} value={}",
                        path, str(value)[:100])
        self._notify(path, value)

    def set_many(self, updates: dict[str, Any]) -> None:
        """批量设置多个配置项, 仅触发一次落盘和一次合并通知.

        比 set() 循环调用避免 N 次原子写盘和 N 次 watcher 回调.

        Args:
            updates: {点分路径: 值} 字典
        """
        with self._lock:
            for path, value in updates.items():
                parts = path.split(".")
                node = self._data
                for part in parts[:-1]:
                    if part not in node or not isinstance(node[part], dict):
                        child_path = f"{getattr(node, '_track_path', '')}.{part}"
                        node[part] = _TrackedDict(_track_path=child_path)
                    node = node[part]
                node[parts[-1]] = value
            self._save()
        # 合并通知：取所有路径的最长公共前缀，避免重复回调
        if updates:
            paths = list(updates.keys())
            common_prefix = paths[0]
            for p in paths[1:]:
                while not p.startswith(common_prefix):
                    common_prefix = common_prefix.rsplit(".", 1)[0]
                if not common_prefix:
                    break
            notify_path = common_prefix or paths[0]
            # 对 models. 路径记录审计日志
            if notify_path.startswith("models."):
                logger.info("config_service.models_batch_write paths={}",
                            ",".join(paths))
            self._notify(notify_path, None)

    def delete(self, path: str) -> None:
        """按点分路径删除配置项, 落盘并通知 watcher."""
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                if part not in node:
                    return
                node = node[part]
            node.pop(parts[-1], None)
            self._save()
        self._notify(path, None)

    def _save(self) -> None:
        # 二次防御: 启动完成后，验证 _data["models"] 与 ROUTE_TABLE 一致
        # 如果 _data 被污染（如通过引用变异），在写盘前从 ROUTE_TABLE 恢复
        if self._startup_complete:
            try:
                from model_router import ROUTE_TABLE
                models = self._data.get("models", {})
                saved_routes = models.get("routes", {})
                repaired_tasks: list[str] = []
                # 遍历 ROUTE_TABLE 中所有路由，与 _data 持久化的 routes 对比
                # 覆盖 chat + 所有同步路由（chat_pro/chat_flash/...），不仅限 chat
                for task, rt_entry in ROUTE_TABLE.items():
                    if not isinstance(rt_entry, dict):
                        continue
                    rt_provider = rt_entry.get("client", "")
                    rt_model = rt_entry.get("model", "")
                    if not rt_provider or not rt_model:
                        continue
                    saved_rc = saved_routes.get(task)
                    if isinstance(saved_rc, dict) and (
                        saved_rc.get("client") != rt_provider
                        or saved_rc.get("model") != rt_model):
                        # 检测到不一致：从 ROUTE_TABLE 恢复
                        repaired_tasks.append(task)
                        saved_routes[task] = {
                            "model": rt_model,
                            "client": rt_provider,
                            "max_tokens": rt_entry.get("max_tokens"),
                            "thinking": bool(
                                rt_entry.get("thinking")
                                and isinstance(rt_entry.get("thinking"), dict)
                                and rt_entry["thinking"].get("type") == "enabled"
                            ),
                        }
                # 同步 chat_model 字段与 ROUTE_TABLE["chat"] 一致
                chat_route = ROUTE_TABLE.get("chat", {})
                rt_provider = chat_route.get("client", "")
                rt_model = chat_route.get("model", "")
                if rt_provider and rt_model:
                    saved_cm = models.get("chat_model", {})
                    if isinstance(saved_cm, dict) and (
                        saved_cm.get("provider") != rt_provider
                        or saved_cm.get("model_id") != rt_model):
                        repaired_tasks.append("chat_model")
                        models["chat_model"] = {"provider": rt_provider, "model_id": rt_model}
                if repaired_tasks:
                    logger.warning(
                        "config_service.save_inconsistency_repair "
                        "repaired_tasks={} route_table_chat={}/{} — "
                        "restoring _data from ROUTE_TABLE before save",
                        repaired_tasks, rt_provider, rt_model,
                    )
            except ImportError:
                pass  # model_router 不可用（如测试环境）
            except Exception as e:
                logger.debug("config_service.save_validation_error error={}", str(e))

        try:
            from utils.atomic_write import atomic_write
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except Exception:
            logger.debug("config_service.atomic_write_fallback", exc_info=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def watch(self, prefix: str, callback: Callable[[Any], None]) -> None:
        """注册监听器, 路径前缀匹配时回调.

        Args:
            prefix: 点分路径前缀
            callback: 值变更回调函数
        """
        self._watchers.setdefault(prefix, []).append(callback)

    def _notify(self, path: str, value: Any) -> None:
        for prefix, cbs in self._watchers.items():
            if path.startswith(prefix):
                for cb in cbs:
                    try:
                        cb(value)
                    except Exception as e:
                        logger.warning("config_service.watcher_error prefix={} error={}", prefix, str(e))


_instance: ConfigService | None = None
_instance_lock = threading.Lock()


def get_config_service() -> ConfigService:
    """获取全局 ConfigService 单例."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ConfigService()
    return _instance
