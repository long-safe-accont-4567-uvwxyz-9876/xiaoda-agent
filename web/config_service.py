"""ConfigService — Web UI 可改配置的统一存取层（R8/R22）。

所有 UI 修改的配置写入 config/webui_overrides.json（原子写盘），
并立即触发注册的回调使内存对象热生效。密钥永不进入此文件。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from web.node_registry import NODES as FUNCTIONAL_NODES

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
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("config_service.mutation_logging_failed error={}", str(e))

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


def _env_allowed_storage_roots() -> list[str]:
    """解析 LOCAL_AI_ALLOWED_STORAGE_ROOTS（逗号分隔）为路径列表。

    空/未设置返回 []（非受限模式）；每个条目去除首尾空白，空条目丢弃。
    分隔符风格与项目其他列表型环境变量（如 OWNER_IDS）一致。
    """
    raw = os.getenv("LOCAL_AI_ALLOWED_STORAGE_ROOTS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]

_DEFAULTS: dict[str, Any] = {
    "schedule": {
        "enabled": True,
        "greeting_max_per_day": 3,
        "dnd_periods": [{"start": "23:00", "end": "08:00"}],
    },
    "tts": {"auto_speak": False, "default_voice": "xiaoda"},
    "ui": {
        "particles": "medium",
        "tilt3d": True,
        "dendro_cursor_trail": False,  # 鼠标移动拖尾（草粒子轨迹），默认关闭，可在系统设置开启
    },
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
    # 多平台共用上下文：默认关闭（shared_platforms 为空 → 各平台独立会话）。
    # shared_platforms 为参与共享的平台名列表（web/cli/qq/wechat），
    # 被选中的平台在恢复/写入历史时统一映射到 shared_key 对应的共享上下文键，
    # 从而跨平台读同一份历史。QQ 群聊非主人对话始终独立，不纳入共享。
    "context": {
        "shared_platforms": [],
        "shared_key": "shared",
    },
    "retrieval": {},
    "prompt_profiles": {"staging": {}, "production": {}, "history": {}},
    # 本地部署：embedding 引擎模式（""=跟随 env EMBED_MODE；local/remote=用户显式选择）
    # device：算力设备（""=跟随 LOCAL_EMBED_BACKEND/auto；cpu/npu=用户显式选择，重启生效）
    # nodes：功能节点后端选择（auto/local/api/off，主 LLM 除外；默认 api=走硅基流动免费模型）
    "local_deploy": {
        "schema_version": 1,
        "mode": "",
        "device": "",
        "nodes": {node["id"]: node["default"] for node in FUNCTIONAL_NODES},
        "node_models": {},
    },
    # Local AI Platform (Task 6): default_model_root 是用户在 WebUI 选择并持久化的
    # 模型下载根目录（PUT /api/v1/local-ai/storage/default 写入）；
    # restricted_roots 是禁止作为下载目标的路径前缀列表（用户可扩展，默认空）；
    # allowed_storage_roots 是受限模式下允许的存储根目录列表（默认空 = 非受限，
    # 行为与旧版一致；非空时下载/存储目标必须位于这些根目录或其子目录内）。
    # 部署层可用环境变量 LOCAL_AI_ALLOWED_STORAGE_ROOTS（逗号分隔）锁定该列表，
    # env 优先级高于 webui_overrides.json（与 METRICS_ENABLED 同策略）。
    "local_ai": {
        "default_model_root": "",
        "restricted_roots": [],
        "allowed_storage_roots": [],
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
        self._load()
        # Env 优先级高于 webui_overrides.json（与 METRICS_ENABLED 同策略）：
        # LOCAL_AI_ALLOWED_STORAGE_ROOTS 是部署层锁定的受限存储根目录，
        # 非空时开启受限模式，不应被 Web UI 覆盖放宽。env 值不落盘。
        env_roots = _env_allowed_storage_roots()
        if env_roots:
            self._assign("local_ai.allowed_storage_roots", env_roots)

    def _load(self) -> None:
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                # deep_merge 后重新包装为 TrackedDict，确保所有嵌套层都被追踪
                self._deep_merge(self._data, saved)
                # 重新包装以确保加载的数据也是 TrackedDict
                self._data = _wrap_tracked(dict(self._data), "root")
            except (json.JSONDecodeError, OSError, ValueError) as e:
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

    @staticmethod
    def _detach(value: Any) -> Any:
        """脱离调用方引用：容器类型做深拷贝，标量原样返回。

        根因：set("ui.items", lst) 后调用方 lst.append(...) 会直接改变
        服务内存状态，并在下次任意写入时被 _save 持久化（引用污染）。
        models.* 曾出现同类问题，这里对所有配置段统一防御。
        """
        if isinstance(value, (dict, list)):
            return copy.deepcopy(value)
        return value

    def _assign(self, path: str, value: Any) -> None:
        """按点分路径写入 _data（调用方需持锁）。中间节点缺失时创建 _TrackedDict。"""
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            # setdefault 创建的 dict 也必须是 TrackedDict
            if part not in node or not isinstance(node[part], dict):
                child_path = f"{getattr(node, '_track_path', '')}.{part}"
                node[part] = _TrackedDict(_track_path=child_path)
            node = node[part]
        node[parts[-1]] = self._detach(value)

    def _get_nested(self, path: str) -> Any:
        """读取路径的原始值（用于 _save 失败时回滚，不做 models. 深拷贝优化）。

        与 get() 的区别：get() 对 models. 路径返回 json 深拷贝（防引用污染），
        _get_nested 返回原始引用，调用方需自行深拷贝（回滚场景在锁内，无并发风险）。
        """
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        """按点分路径设置配置项, 落盘并通知 watcher.

        Args:
            path: 点分路径
            value: 新值

        Qodo#2 修复：_save() 失败时回滚 _data，防止内存数据被污染。
        旧实现 _assign 先改 _data 再 _save，_save 失败时 _data 已是新值，
        后续任意写入会把污染的 _data 持久化（registry 只回滚 ROUTE_TABLE 不够）。
        """
        with self._lock:
            old_value = copy.deepcopy(self._get_nested(path))
            self._assign(path, value)
            try:
                self._save()
            except (OSError, RuntimeError):
                # _save 失败：回滚 _data 到旧值，防止内存污染
                self._assign(path, old_value)
                logger.error("config_service.set_rollback path={} reason=save_failed", path)
                raise
        # 审计日志：models. 路径写入。含调用栈，用于定位模型配置被反复覆盖为 mimo 的根因。
        if path.startswith("models."):
            _stack = "".join(traceback.format_stack(limit=12)[:-1])
            logger.info("config_service.models_write path={} value={} stack=\n{}",
                        path, str(value)[:100], _stack)
        self._notify(path, value)

    def set_many(self, updates: dict[str, Any]) -> None:
        """批量设置多个配置项, 仅触发一次落盘和逐路径通知.

        比 set() 循环调用避免 N 次原子写盘，但仍逐路径通知 watcher
        以保证与 set() 的通知语义一致（每个路径的 watcher 都收到回调）。

        Args:
            updates: {点分路径: 值} 字典

        Qodo#2 修复：_save() 失败时回滚所有已修改路径的 _data。
        """
        with self._lock:
            # 保存所有路径的旧值（深拷贝），用于 _save 失败时回滚
            old_values = {p: copy.deepcopy(self._get_nested(p)) for p in updates}
            for path, value in updates.items():
                self._assign(path, value)
            try:
                self._save()
            except (OSError, RuntimeError):
                # _save 失败：回滚所有已修改路径
                for path, old in old_values.items():
                    self._assign(path, old)
                logger.error("config_service.set_many_rollback paths={} reason=save_failed",
                             ",".join(updates.keys()))
                raise
        # 逐路径通知：保证每个路径的 watcher 都收到回调，语义与 set() 一致
        # 只省略了中间的 N-1 次 _save()，通知仍然逐条发送
        if updates:
            # 对 models. 路径记录审计日志
            models_paths = [p for p in updates if p.startswith("models.")]
            if models_paths:
                _stack = "".join(traceback.format_stack(limit=12)[:-1])
                logger.info("config_service.models_batch_write paths={} stack=\n{}",
                            ",".join(models_paths), _stack)
            for path, value in updates.items():
                self._notify(path, value)

    def replace_many(
        self, updates: dict[str, Any], deletes: list[str]
    ) -> None:
        """在一次原子写盘中批量设置并删除路径。"""
        touched = set(updates) | set(deletes)
        with self._lock:
            old_values = {
                path: copy.deepcopy(self._get_nested(path)) for path in touched
            }
            for path, value in updates.items():
                self._assign(path, value)
            for path in deletes:
                parts = path.split(".")
                node: Any = self._data
                for part in parts[:-1]:
                    if not isinstance(node, dict) or part not in node:
                        node = None
                        break
                    node = node[part]
                if isinstance(node, dict):
                    node.pop(parts[-1], None)
            try:
                self._save()
            except (OSError, RuntimeError):
                for path, value in old_values.items():
                    if value is None:
                        parts = path.split(".")
                        node = self._data
                        for part in parts[:-1]:
                            if not isinstance(node, dict) or part not in node:
                                node = None
                                break
                            node = node[part]
                        if isinstance(node, dict):
                            node.pop(parts[-1], None)
                    else:
                        self._assign(path, value)
                raise
        for path, value in updates.items():
            self._notify(path, value)
        for path in deletes:
            self._notify(path, None)

    def delete(self, path: str) -> None:
        """按点分路径删除配置项, 落盘并通知 watcher.

        Warning 4/Item 2 修复：_save() 失败时恢复内存快照（删除前的旧值），
        防止内存已被删除但磁盘未删除导致的脏状态被后续写入持久化。
        """
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                # 中间节点被标量遮蔽时（如 ui.particles="low" 再删 ui.particles.inner）
                # 原实现会抛 AttributeError，公共 API 应对不存在的路径安全返回
                if not isinstance(node, dict) or part not in node:
                    return
                node = node[part]
            if not isinstance(node, dict):
                return
            old_value = node.get(parts[-1])
            existed = parts[-1] in node
            node.pop(parts[-1], None)
            try:
                self._save()
            except (OSError, RuntimeError):
                if existed:
                    self._assign(path, copy.deepcopy(old_value))
                logger.error("config_service.delete_rollback path={} reason=save_failed", path)
                raise
        self._notify(path, None)

    def _save(self) -> None:
        # _save 只做原子写盘，不再反向同步 ROUTE_TABLE。
        # 历史背景：原实现从 ROUTE_TABLE 反向恢复 _data（防止引用变异污染），
        # 但 mark_startup_complete 从未在生产中调用，该逻辑是死代码。
        # 重构后：ROUTE_TABLE 由 ModelRouteRegistry 管理，所有修改走原子入口，
        # ConfigService 是唯一真相源，不需要反向同步。
        try:
            from utils.atomic_write import atomic_write
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except (OSError, RuntimeError):
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
                    except (RuntimeError, ValueError, OSError) as e:
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
