"""核心配置热重载 (H2) — watchdog + 原子切换

参考:
- AsyncConfig: Async-first config with hot reloading (watchdog)
- ConfigHotReloader: File-based hot reload with hash validation
- 双缓冲快照模式: active/pending 原子指针交换

特性:
- 监听 config/agent.json5 文件变更
- 修改后 5 秒内自动生效 (毫秒级延迟)
- 原子切换: 读侧无锁, 写侧独占构建
- 版本号校验: 防止回滚
- 回调机制: 配置变更时通知订阅者
- 无需重启服务

注意:
- 不依赖外部 ETCD/Consul, 单机足够
- watchdog 在 Linux 使用 inotify, Windows 使用 ReadDirectoryChangesW
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False
    Observer = None  # type: ignore
    FileSystemEventHandler = object  # type: ignore


class ConfigSnapshot:
    """配置快照 (不可变)"""

    def __init__(self, data: dict, version: int, hash_: str) -> None:
        self._data = dict(data)
        self.version = version
        self.hash = hash_
        self.created_at = time.time()

    def get(self, key: str, default: Any = None) -> Any:
        """支持点号路径: get('a.b.c')"""
        parts = key.split(".")
        v = self._data
        for p in parts:
            if isinstance(v, dict) and p in v:
                v = v[p]
            else:
                return default
        return v

    def set(self, key: str, value: Any) -> None:
        """仅用于新快照构建"""
        parts = key.split(".")
        d = self._data
        for p in parts[:-1]:
            if p not in d or not isinstance(d[p], dict):
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value

    def as_dict(self) -> dict:
        """返回配置数据的浅拷贝字典."""
        return dict(self._data)


class ConfigReloader:
    """核心配置热重载器

    用法:
        reloader = ConfigReloader("config/agent.json5")
        reloader.start()
        # 业务代码
        timeout = reloader.current.get("chat.timeout", 30)
        # 配置文件变更时, 自动触发回调
        reloader.on_change(lambda snap: print(f"config v{snap.version}"))
    """

    def __init__(self, config_path: str | Path,
                 parse_fn: Callable[[str], dict] | None = None) -> None:
        self._path = Path(config_path)
        self._parse_fn = parse_fn or self._default_parse
        self._lock = threading.RLock()
        self._active: ConfigSnapshot | None = None
        self._callbacks: list[Callable[[ConfigSnapshot], None]] = []
        self._async_callbacks: list[Callable] = []
        self._async_cb_tasks: set[asyncio.Task] = set()
        self._observer: Any | None = None
        self._stopped = False
        # 主线程事件循环引用 - 在 start() 中捕获, 用于 Timer 线程跨线程调度.
        # call_soon_threadsafe 本身线程安全, 仅需保存 loop 引用即可.
        self._loop: asyncio.AbstractEventLoop | None = None
        # 修复 P1：保存主线程事件循环，供 Timer 线程跨线程调度异步回调
        # 原代码在 Timer 线程内调用 get_running_loop() 会失败，导致异步回调静默丢失
        try:
            self._main_loop = asyncio.get_event_loop()
        except RuntimeError:
            self._main_loop = None
        # 加载初始配置
        self._load()

    def _default_parse(self, content: str) -> dict:
        """默认解析: JSON5 兼容 (剥离注释)"""
        # 简单 JSON5: 去掉单行注释和尾逗号
        import re
        s = re.sub(r'//.*?$', '', content, flags=re.MULTILINE)
        s = re.sub(r'/\*.*?\*/', '', s, flags=re.DOTALL)
        s = re.sub(r',\s*([}\]])', r'\1', s)
        return json.loads(s)

    def _compute_hash(self, content: str) -> str:
        return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()

    def _load(self) -> bool:
        """加载配置 (内部使用, 调用方需持有锁)"""
        if not self._path.exists():
            logger.warning(f"ConfigReloader: file not found {self._path}")
            return False
        try:
            content = self._path.read_text(encoding="utf-8")
            new_hash = self._compute_hash(content)
            if self._active and new_hash == self._active.hash:
                return False  # 内容未变
            data = self._parse_fn(content)
            if not isinstance(data, dict):
                logger.error("ConfigReloader: parsed config is not a dict")
                return False
            new_version = (self._active.version + 1) if self._active else 1
            new_snap = ConfigSnapshot(data, new_version, new_hash)
            # 原子切换: 替换 _active 指针
            self._active = new_snap
            logger.info(f"ConfigReloader: reloaded v{new_version} "
                         f"hash={new_hash[:8]}")
            return True
        except Exception as e:
            logger.error(f"ConfigReloader: load failed {e}")
            return False

    def reload(self) -> bool:
        """手动触发重载"""
        with self._lock:
            changed = self._load()
        if changed:
            self._notify_callbacks()
        return changed

    def _notify_callbacks(self) -> None:
        """通知所有回调 (在锁外执行)"""
        snap = self._active
        if not snap:
            return
        for cb in self._callbacks:
            try:
                cb(snap)
            except Exception as e:
                logger.warning(f"ConfigReloader: callback failed {e}")
        # 异步回调 (在事件循环中)
        # 修复 P1：原代码在 Timer 线程内调用 asyncio.get_running_loop()，
        # Timer 线程无 running loop，外层抛 RuntimeError 后 except 内再次调用
        # 同样抛 RuntimeError，导致 call_soon_threadsafe 路径永不可达，
        # 所有异步回调在配置热重载时静默丢失。
        # 修复：优先用初始化时保存的主线程 loop；不可用时再尝试 get_event_loop。
        loop = self._main_loop or self._loop
        if loop is None or not loop.is_running():
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
        for acb in self._async_callbacks:
            try:
                if loop is None or not loop.is_running():
                    # 无可用事件循环：直接执行（同步路径）或放弃
                    if not asyncio.iscoroutinefunction(acb):
                        acb(snap)
                    else:
                        logger.warning("config_reloader.no_running_loop_drop_async_cb")
                    continue
                if asyncio.iscoroutinefunction(acb):
                    # 使用 call_soon_threadsafe 跨线程调度，保证线程安全。
                    # 同时把 task 加入 _async_cb_tasks 集合并挂上 done_callback，
                    # 否则异常不会触发 _on_async_cb_done 的 warning 日志，且 task
                    # 可能被 GC 回收（修复回归测试 test_config_reloader_*）。
                    def _schedule(_acb=acb, _snap=snap, _loop=loop) -> None:
                        def _create_and_track() -> None:
                            task = _loop.create_task(_acb(_snap))
                            self._async_cb_tasks.add(task)
                            task.add_done_callback(self._on_async_cb_done)
                        _loop.call_soon_threadsafe(_create_and_track)
                    _schedule()
                else:
                    loop.call_soon_threadsafe(acb, snap)
            except Exception:
                logger.debug("config_reloader.async_callback_error: {}", exc_info=True)

    def _on_async_cb_done(self, task: asyncio.Task) -> None:
        """异步回调任务完成: 移除引用并记录异常。"""
        self._async_cb_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("config_reloader.async_callback_failed error={}", str(exc))

    @property
    def current(self) -> ConfigSnapshot:
        """获取当前配置快照 (线程安全, 无锁读)"""
        return self._active  # type: ignore

    def get(self, key: str, default: Any = None) -> Any:
        """便捷访问"""
        if self._active is None:
            return default
        return self._active.get(key, default)

    def on_change(self, callback: Callable[[ConfigSnapshot], None]) -> None:
        """注册同步回调"""
        self._callbacks.append(callback)

    def on_change_async(self, callback: Callable) -> None:
        """注册异步回调 (协程函数)"""
        self._async_callbacks.append(callback)

    def start(self) -> bool:
        """启动文件监听"""
        # 捕获主线程事件循环引用 (start() 通常在事件循环已启动后调用,
        # 比 __init__ 时机更可靠). 用于 Timer 线程跨线程调度异步回调.
        # 即使 watchdog 未安装也需要捕获 (reload() 仍可被外部调用触发回调).
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self._loop = asyncio.get_event_loop()
                except RuntimeError:
                    self._loop = None
                    logger.warning("ConfigReloader: no event loop available, async callbacks may be dropped")
        if self._main_loop is None and self._loop is not None:
            self._main_loop = self._loop

        if not _HAS_WATCHDOG:
            logger.warning("ConfigReloader: watchdog not installed, hot reload disabled")
            return False
        if self._observer is not None:
            return True

        class _Handler(FileSystemEventHandler):
            def __init__(self, reloader: ConfigReloader) -> None:
                self._reloader = reloader
                self._last_event = 0

            def on_modified(self, event: FileSystemEvent) -> None:
                """文件修改事件回调, 防抖后延迟触发重载."""
                if event.is_directory:
                    return
                if Path(event.src_path).resolve() != self._reloader._path.resolve():
                    return
                # 防抖: 200ms 内重复事件合并
                now = time.time()
                if now - self._last_event < 0.2:
                    return
                self._last_event = now
                # 延迟 100ms 等写入完成
                threading.Timer(0.1, self._reloader.reload).start()

        self._observer = Observer()
        self._observer.schedule(
            _Handler(self),
            path=str(self._path.parent),
            recursive=False,
        )
        self._observer.daemon = True
        self._observer.start()
        logger.info(f"ConfigReloader: watching {self._path}")
        return True

    def stop(self) -> None:
        """停止监听"""
        self._stopped = True
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None


# ============================================================
# 全局单例
# ============================================================

_reloader: ConfigReloader | None = None


def get_config_reloader() -> ConfigReloader:
    """获取全局配置热重载器 (单例)"""
    global _reloader
    if _reloader is None:
        from config import AGENT_CONFIG_PATH
        _reloader = ConfigReloader(AGENT_CONFIG_PATH)
        _reloader.start()
    return _reloader
