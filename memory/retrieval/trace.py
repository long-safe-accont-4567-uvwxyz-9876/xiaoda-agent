from collections.abc import Awaitable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NamedTuple


@dataclass(frozen=True)
class RetrievalOutcome:
    results: tuple[dict, ...]
    degraded_components: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...]


class ChannelOutcome(NamedTuple):
    """gather 子任务的通道结果 + 该任务内新增的 degraded 打点。

    asyncio.gather 会把每个通道协程包成独立 Task（contextvars 快照拷贝），
    通道内部 mark_retrieval_degraded() 只改子任务自己的副本，父协程读不到
    （B1 根因：scope_scan_partial/kg_v2 信号在七路召回处系统性丢失）。
    capture_channel_trace 先快照、结束时取差集，父协程 gather 后用
    merge_channel_outcomes 单点合并回本次请求自己的上下文，不会跨请求串味。
    """
    result: Any
    degraded: tuple[str, ...]


async def capture_channel_trace(awaitable: Awaitable[Any]) -> ChannelOutcome:
    """运行通道协程并捕获其任务上下文内新增的 degraded 打点。

    协程抛异常时原样上抛（此时无结果可带，打点随异常路径丢失，与旧行为一致）。
    """
    baseline = frozenset(read_retrieval_trace())
    result = await awaitable
    produced = tuple(
        component for component in read_retrieval_trace()
        if component not in baseline
    )
    return ChannelOutcome(result, produced)


def merge_channel_outcomes(outcomes: Iterable[ChannelOutcome]) -> None:
    """把子任务捕获到的 degraded 打点合并进当前（调用方）上下文。

    约定只在 gather 返回后的父协程里单点调用：asyncio 单线程协作式调度下
    顺序合并没有竞态；mark_retrieval_degraded 自带去重。
    """
    for outcome in outcomes:
        for component in outcome.degraded:
            mark_retrieval_degraded(component)

_retrieval_degraded: ContextVar[tuple[str, ...]] = ContextVar(
    "retrieval_degraded", default=()
)
_retrieval_dropped: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "retrieval_dropped", default=()
)


def begin_retrieval_trace() -> None:
    _retrieval_degraded.set(())
    _retrieval_dropped.set(())


def mark_retrieval_degraded(component: str) -> None:
    current = _retrieval_degraded.get()
    if component not in current:
        _retrieval_degraded.set((*current, component))


def mark_retrieval_dropped(source_id: object, reason: str) -> None:
    item = (str(source_id or ""), reason)
    current = _retrieval_dropped.get()
    if item not in current:
        _retrieval_dropped.set((*current, item))


def read_retrieval_trace() -> tuple[str, ...]:
    return _retrieval_degraded.get()


def read_retrieval_dropped() -> tuple[tuple[str, str], ...]:
    return _retrieval_dropped.get()
