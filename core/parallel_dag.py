"""并行工具调用 DAG (P5) — 无依赖工具并发执行

参考:
- Gemini API Parallel Function Calling (Production patterns)
- DAG-based Agent Tool Scheduling (CSDN)
- asyncio.gather with return_exceptions

特性:
- 自动解析工具依赖 (基于 output_to/input_from 声明)
- 入度为 0 的节点并发执行 (asyncio.gather)
- 部分失败容错 (return_exceptions=True)
- 信号量限制并发上限
- 状态机: PENDING → RUNNING → SUCCESS / FAILED
- 支持重试与降级
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from loguru import logger



class NodeState(str, Enum):
    """DAG 节点执行状态枚举。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DAGNode:
    """DAG 节点 — 一个工具调用"""
    name: str
    handler: Callable
    args: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    timeout: float = 60.0
    retries: int = 1
    fallback: Optional[Callable] = None
    state: NodeState = NodeState.PENDING
    result: Any = None
    error: Optional[Exception] = None
    started_at: float = 0
    finished_at: float = 0

    @property
    def duration(self) -> float:
        """返回节点执行耗时 (秒), 未完成时返回 0."""
        if self.finished_at and self.started_at:
            return self.finished_at - self.started_at
        return 0


@dataclass
class DAGResult:
    """DAG 执行结果"""
    nodes: dict[str, DAGNode]
    total_duration: float
    success_count: int
    failed_count: int
    skipped_count: int
    outputs: dict[str, Any]

    @property
    def is_all_success(self) -> bool:
        """返回所有节点是否均成功 (无失败节点)."""
        return self.failed_count == 0

    def get(self, name: str, default: Any=None) -> Any:
        """按节点名获取成功结果, 失败或不存在时返回默认值.

        Args:
            name: 节点名
            default: 节点不存在或未成功时的默认返回值

        Returns:
            节点成功结果, 否则返回 default
        """
        node = self.nodes.get(name)
        return node.result if node and node.state == NodeState.SUCCESS else default


class ToolDAG:
    """工具调用 DAG — 并行编排

    用法:
        dag = ToolDAG()
        dag.add_node("search", search_handler, args={"q": "weather"})
        dag.add_node("fetch_img", img_handler, depends_on=["search"])
        dag.add_node("render", render_handler, depends_on=["fetch_img"])
        result = await dag.execute()
    """

    def __init__(self, max_concurrency: int = 8) -> None:
        self._nodes: dict[str, DAGNode] = {}
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # per-wait-cycle Event 池, 避免 set()/clear() 时序竞争
        self._waiters: list[asyncio.Event] = []

    def add_node(self, name: str, handler: Callable,
                  args: Optional[dict] = None,
                  depends_on: Optional[list[str]] = None,
                  produces: Optional[list[str]] = None,
                  consumes: Optional[list[str]] = None,
                  timeout: float = 60.0, retries: int = 1,
                  fallback: Optional[Callable] = None) -> "ToolDAG":
        """添加节点"""
        self._nodes[name] = DAGNode(
            name=name, handler=handler,
            args=args or {},
            depends_on=list(depends_on or []),
            produces=list(produces or []),
            consumes=list(consumes or []),
            timeout=timeout, retries=retries, fallback=fallback,
        )
        return self

    def _validate(self) -> None:
        """校验 DAG 合法性 (无环, 依赖存在)"""
        for name, node in self._nodes.items():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    raise ValueError(f"DAG invalid: node '{name}' depends on unknown '{dep}'")
        # 简单环检测
        visited = set()
        stack = set()

        def visit(n: str) -> None:
            """深度优先遍历节点依赖, 检测是否存在环."""
            if n in stack:
                raise ValueError(f"DAG cycle detected at '{n}'")
            if n in visited:
                return
            stack.add(n)
            for dep in self._nodes[n].depends_on:
                visit(dep)
            stack.remove(n)
            visited.add(n)

        for n in self._nodes:
            visit(n)

    def _ready_nodes(self) -> list[DAGNode]:
        """返回入度为 0 且未执行的节点"""
        return [
            n for n in self._nodes.values()
            if n.state == NodeState.PENDING
            and all(self._nodes[d].state == NodeState.SUCCESS
                     for d in n.depends_on)
        ]

    async def _execute_node(self, node: DAGNode,
                              context: dict[str, Any]) -> None:
        """执行单个节点 (含重试 + 降级)"""
        node.started_at = time.time()
        node.state = NodeState.RUNNING

        # 注入依赖产出 (直接合并到 args 中, 便于 handler 接收)
        for c in node.consumes:
            if c in context:
                node.args.setdefault(c, context[c])

        last_error = None
        for attempt in range(node.retries + 1):
            try:
                async with self._semaphore:
                    async def _run() -> Any:
                        # 先调用获取结果, 判断是 coroutine 还是普通返回
                        result = node.handler(**node.args)
                        if asyncio.iscoroutine(result):
                            return await asyncio.wait_for(result, timeout=node.timeout)
                        return result
                    node.result = await _run()
                    node.state = NodeState.SUCCESS
                    node.finished_at = time.time()
                    # 把产出放入上下文
                    for p in node.produces:
                        if isinstance(node.result, dict) and p in node.result:
                            context[p] = node.result[p]
                        else:
                            context[p] = node.result
                    logger.debug(f"DAG.node.success name={node.name} "
                                  f"duration={node.duration:.3f}s attempt={attempt+1}")
                    self._signal_done()
                    return
            except Exception as e:
                last_error = e
                logger.warning(f"DAG.node.failed name={node.name} "
                                f"attempt={attempt+1} error={e}")
                if attempt < node.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))

        # 降级
        if node.fallback is not None:
            try:
                if asyncio.iscoroutinefunction(node.fallback):
                    node.result = await node.fallback(**node.args)
                else:
                    node.result = await asyncio.to_thread(node.fallback, **node.args)
                node.state = NodeState.SUCCESS
                node.finished_at = time.time()
                for p in node.produces:
                    context[p] = node.result
                logger.info(f"DAG.node.fallback_success name={node.name}")
                self._signal_done()
                return
            except Exception as e:
                last_error = e

        node.state = NodeState.FAILED
        node.error = last_error
        node.finished_at = time.time()
        # 把失败节点的下游标记为 SKIPPED
        self._skip_downstream(node.name)
        self._signal_done()

    def _skip_downstream(self, name: str) -> None:
        """递归跳过失败节点的所有下游"""
        for n in self._nodes.values():
            if name in n.depends_on and n.state == NodeState.PENDING:
                n.state = NodeState.SKIPPED
                self._skip_downstream(n.name)

    def _signal_done(self) -> None:
        """唤醒所有等待中的 execute 循环（per-wait-cycle Event 模式）"""
        for evt in self._waiters:
            evt.set()

    async def execute(self) -> DAGResult:
        """执行整个 DAG"""
        self._validate()
        t0 = time.time()
        context: dict[str, Any] = {}

        while True:
            ready = self._ready_nodes()
            if not ready:
                # 检查是否所有节点都已完成
                pending = [n for n in self._nodes.values()
                            if n.state in (NodeState.PENDING, NodeState.RUNNING)]
                if not pending:
                    break
                # per-wait-cycle Event: 每次等待创建独立 Event, 避免 set/clear 竞争
                evt = asyncio.Event()
                self._waiters.append(evt)
                try:
                    await asyncio.wait_for(evt.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    if evt in self._waiters:
                        self._waiters.remove(evt)
                continue

            # 并发执行所有就绪节点
            tasks = [self._execute_node(n, context) for n in ready]
            await asyncio.gather(*tasks, return_exceptions=True)

        total = time.time() - t0
        success = sum(1 for n in self._nodes.values() if n.state == NodeState.SUCCESS)
        failed = sum(1 for n in self._nodes.values() if n.state == NodeState.FAILED)
        skipped = sum(1 for n in self._nodes.values() if n.state == NodeState.SKIPPED)

        return DAGResult(
            nodes=dict(self._nodes),
            total_duration=total,
            success_count=success,
            failed_count=failed,
            skipped_count=skipped,
            outputs=context,
        )

    def visualize(self) -> str:
        """简单 ASCII 可视化"""
        lines = ["ToolDAG:"]
        for name, node in self._nodes.items():
            deps = ", ".join(node.depends_on) or "(root)"
            lines.append(f"  {deps} → [{name}] state={node.state.value}")
        return "\n".join(lines)
