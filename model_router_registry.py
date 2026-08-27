"""model_router 的路由表注册中心 — 自 model_router.py 拆分（上帝文件 Phase 2）。

ModelRouteRegistry 是 ROUTE_TABLE 的唯一读写入口（原子更新 + 持久化回滚 +
深拷贝快照），函数体自 model_router.py 逐字节搬移，仅缩进调整。

兼容契约（tests/test_model_router_registry_module.py）：
    - 本模块不得 import model_router（防循环依赖）
    - model_router 同名 re-export，`from model_router import ModelRouteRegistry`
      等既有用法不受影响
"""
from __future__ import annotations

import copy
from typing import Any

from loguru import logger

# 降级链表（task → fallback task）。原住 model_router.py 门面，技术债批下沉：
# llm_gateway/fallback_chain.py 的兼容契约禁止 import model_router（防循环依赖），
# 但降级策略数据此前仍留在门面模块，迫使网关层函数内反向 import、契约名存实亡。
# 本模块不依赖任何上层模块，是路由数据的规范住所；门面与网关均从这里取值。
FALLBACK_ROUTE: dict[str, str] = {
    # chat_pro/chat_flash 已合并进 chat（同一 provider 同一 model，无区分意义）
    # 降级链：chat 失败 → chat_agnes（agnes provider 作为独立兜底）
    "chat": "chat_agnes",
}


class ModelRouteRegistry:
    """路由表注册中心：ROUTE_TABLE 的唯一读写入口。

    设计原则：
    - 启动后 _table 是只读快照，所有修改必须走 update_route()
    - update_route() 是原子操作：构造新 entry → 写内存 → 持久化 → 失败回滚
    - get_task/snapshot_task 返回深拷贝，防引用污染
    - replace_table 用于启动时一次性填充（不持久化，由调用方负责）

    这样保证：
    1. 用户改过的配置不会被降级链/fallback 路径覆盖
    2. 持久化失败不留半成品状态
    3. 降级链读取的是独立快照，修改不影响全局
    """

    def __init__(self, initial_table: dict | None = None,
                 config_service: Any = None) -> None:
        # 直接引用传入的 table（不深拷贝）：
        # 生产中传入 ROUTE_TABLE，registry._table 就是 ROUTE_TABLE 本身，
        # update_route 修改 self._table[task] 即修改 ROUTE_TABLE[task]，
        # 保证 route() 读 ROUTE_TABLE 时拿到最新值（避免 registry 与 ROUTE_TABLE 脱节）。
        # 测试中传入局部 dict，修改不影响全局；如需隔离，调用方自行深拷贝后传入。
        self._table: dict[str, dict] = initial_table if initial_table is not None else {}
        # 延迟加载 ConfigService：测试时可注入 mock，生产时从 get_config_service() 取
        self._cfg = config_service

    def _get_cfg(self) -> Any:
        """延迟获取 ConfigService 实例（避免循环导入）。"""
        if self._cfg is not None:
            return self._cfg
        try:
            from core_runtime.config_service import get_config_service
            self._cfg = get_config_service()
        except (ImportError, RuntimeError) as e:
            logger.warning("registry.config_service_unavailable error={}", str(e))
            self._cfg = None
        return self._cfg

    def get_task(self, task: str) -> dict | None:
        """返回指定 task 路由的深拷贝（调用方修改不影响内部状态）。"""
        entry = self._table.get(task)
        return copy.deepcopy(entry) if entry is not None else None

    def snapshot_task(self, task: str) -> dict | None:
        """同 get_task，语义上表示"用于构造 fallback 的快照"。"""
        return self.get_task(task)

    def all_tasks(self) -> list[str]:
        """返回所有 task 名称。"""
        return list(self._table.keys())

    def replace_table(self, new_table: dict) -> None:
        """启动时一次性替换整个表（不触发持久化）。

        用于 _apply_route_overrides：从 ConfigService 加载用户配置后，
        用持久化值覆盖默认 ROUTE_TABLE。持久化由调用方决定（启动时一般不写回）。

        CodeRabbit#5 修复：保持 self._table 的对象身份不变（clear + update），
        而非重新赋值 self._table = new_dict。生产中 self._table 就是模块级
        ROUTE_TABLE 本身，route()/chat_stream() 等请求路径仍直接读 ROUTE_TABLE；
        若 here 重新赋值，registry 后续 update_route 作用于新 dict，而请求路径
        还在读旧 ROUTE_TABLE → 数据脱节，用户改的配置对在途请求不可见。
        """
        self._table.clear()
        self._table.update(copy.deepcopy(new_table))

    def get_task_ref(self, task: str) -> dict | None:
        """返回指定 task 路由的**引用**（不深拷贝）。

        供热路径 route()/chat_stream()/get_max_tokens_for_task 使用，避免每次
        调用都深拷贝（深拷贝虽是微秒级，但 chat 热路径每秒可能数十次调用）。
        调用方承诺只读不改返回的 dict；修改请走 update_route()。
        """
        return self._table.get(task)

    def update_route(self, task: str, model_id: str, provider: str,
                     max_tokens: int | None = None,
                     thinking: dict | None = None,
                     timeout: int | None = None,
                     persist: bool = True,
                     extra_persist: dict | None = None) -> dict:
        """原子地更新路由：内存 + 持久化。

        Args:
            task: 路由 task 名称（如 "chat"）
            model_id: 模型 ID
            provider: provider 名称
            max_tokens: 可选，max_tokens 上限
            thinking: 可选，{"type": "enabled"|"disabled", "budget_tokens": ...}
            timeout: 可选，超时秒数
            persist: 是否持久化到 ConfigService（启动恢复时设为 False）
            extra_persist: 可选，与路由同一次落盘的额外配置（如 models.chat_model），
                通过 set_many 原子写入，避免第二次写失败导致配置分裂。

        Returns:
            新的路由 entry（深拷贝）

        Raises:
            KeyError: task 不存在
            RuntimeError: 持久化失败（内存已回滚）
        """
        if task not in self._table:
            raise KeyError(f"未知路由 task: {task}")

        # 保留旧值用于回滚
        old_entry = copy.deepcopy(self._table[task])

        # 构造新 entry
        new_entry = self._merge_route_entry(
            old_entry, model_id, provider, max_tokens, thinking, timeout)

        # 写内存
        self._table[task] = new_entry

        # 持久化（失败回滚）
        if persist:
            cfg = self._get_cfg()
            if cfg is not None:
                try:
                    route_value = self._build_route_persist_value(new_entry, timeout)
                    if extra_persist:
                        cfg.set_many({f"models.routes.{task}": route_value, **extra_persist})
                    else:
                        cfg.set(f"models.routes.{task}", route_value)
                except Exception as e:
                    # 回滚内存
                    self._table[task] = old_entry
                    logger.error("registry.update_route_persist_failed task={} error={}",
                                 task, str(e))
                    raise RuntimeError(f"持久化路由 {task} 失败: {e}") from e

        return copy.deepcopy(new_entry)

    @staticmethod
    def _merge_route_entry(old_entry: dict, model_id: str, provider: str,
                           max_tokens: int | None, thinking: dict | None,
                           timeout: int | None) -> dict:
        """基于旧 entry 构造新 entry（deepcopy 后合并新值）。"""
        new_entry = copy.deepcopy(old_entry)
        new_entry["model"] = model_id
        new_entry["client"] = provider
        if max_tokens is not None:
            new_entry["max_tokens"] = max_tokens
        if thinking is not None:
            new_entry["thinking"] = copy.deepcopy(thinking)
        # CodeRabbit#7 修复：timeout 也合并进 new_entry，
        # 这样持久化时 new_entry.get("timeout") 能拿到有效值
        if timeout is not None:
            new_entry["timeout"] = timeout
        return new_entry

    @staticmethod
    def _build_route_persist_value(new_entry: dict, timeout: int | None) -> dict:
        """从 new_entry 构造持久化的 route_value。

        CodeRabbit#3+#9 + Qodo#3 修复：持久化用 new_entry 的有效值（max_tokens/
        thinking/timeout 已继承自旧值），不会再省略字段误存为 false/60。
        """
        _effective_thinking = new_entry.get("thinking")
        _thinking_bool = bool(
            _effective_thinking and isinstance(_effective_thinking, dict)
            and _effective_thinking.get("type") == "enabled"
        )
        # timeout：new_entry 已继承 old_entry.timeout；若 new_entry 无则用入参兜底
        _effective_timeout = new_entry.get("timeout")
        if _effective_timeout is None and timeout is not None:
            _effective_timeout = timeout
        return {
            "model": new_entry["model"],
            "client": new_entry["client"],
            "max_tokens": new_entry.get("max_tokens"),
            "thinking": _thinking_bool,
            "timeout": _effective_timeout,
        }
