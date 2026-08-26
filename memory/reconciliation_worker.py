from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from db.db_memory_reconciliation import ReconciliationDecisionInput, ReconciliationRepository
from memory.reconciliation_models import ReconciliationAction, ReconciliationDecision
from memory.reconciliation_policy import DecisionBatchError, parse_decision_batch

RETRY_DELAYS = (30.0, 120.0, 600.0)
DEFAULT_POLL_INTERVAL = 30.0
MAX_POLL_BACKOFF = 600.0
MIN_ERROR_BACKOFF = 1.0
logger = logging.getLogger(__name__)


class ReconciliationMode(str, Enum):
    SHADOW = "shadow"
    ENFORCE = "enforce"


class DecisionProvider(Protocol):
    def __call__(self, decision_input: ReconciliationDecisionInput) -> Awaitable[str]: ...


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: str
    job_id: int | None = None
    action: ReconciliationAction | None = None


class ReconciliationWorker:
    def __init__(
        self,
        repository: ReconciliationRepository,
        decision_provider: DecisionProvider | Callable[[ReconciliationDecisionInput], Awaitable[str]],
        *,
        user_id: str,
        agent_id: str,
        mode: ReconciliationMode = ReconciliationMode.SHADOW,
        allowed_actions: set[ReconciliationAction] | None = None,
        lease_seconds: float = 60.0,
    ) -> None:
        self._repository = repository
        self._decision_provider = decision_provider
        self._user_id = user_id
        self._agent_id = agent_id
        self._mode = mode
        self._allowed_actions = frozenset(allowed_actions or set())
        self._lease_seconds = lease_seconds

    async def _record_or_apply(
        self,
        decision_input: ReconciliationDecisionInput,
        decision: ReconciliationDecision,
        *,
        fallback: bool,
    ) -> WorkerResult:
        claim = decision_input.claim
        can_enforce = self._mode is ReconciliationMode.ENFORCE and decision.action in self._allowed_actions
        if not can_enforce:
            try:
                await self._repository.record_shadow(
                    claim.job_id,
                    claim.lease_token,
                    decision,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return await self._schedule_retry(
                    claim,
                    error_code="proposal_record_failed",
                    original_error=exc,
                )
            return WorkerResult(
                status="fallback_shadow" if fallback else "shadow",
                job_id=claim.job_id,
                action=decision.action,
            )

        expected_versions = {
            target_id: decision_input.context.targets[target_id].version
            for target_id in decision.target_ids
        }
        try:
            await self._repository.apply_action(
                claim.job_id,
                claim.lease_token,
                decision,
                expected_versions=expected_versions,
                candidate_expected_version=decision_input.candidate_expected_version,
                candidate_expected_status=decision_input.candidate_expected_status,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._schedule_retry(
                claim,
                error_code="action_apply_failed",
                original_error=exc,
            )
        return WorkerResult(
            status="fallback_applied" if fallback else "applied",
            job_id=claim.job_id,
            action=decision.action,
        )

    async def _schedule_retry(
        self,
        claim,
        *,
        error_code: str,
        original_error: Exception | None = None,
    ) -> WorkerResult:
        retry_delay = RETRY_DELAYS[min(claim.retry_count, len(RETRY_DELAYS) - 1)]
        try:
            scheduled = await self._repository.fail_job(
                claim.job_id,
                claim.lease_token,
                error=error_code,
                retry_delay=retry_delay,
            )
            if not scheduled:
                raise RuntimeError("reconciliation job lease was lost while scheduling retry")
        except Exception as retry_error:
            logger.error(
                "memory.reconciliation_worker.fail_job_failed",
                extra={"job_id": claim.job_id, "stage": error_code},
            )
            if original_error is not None:
                raise original_error from retry_error
            raise
        return WorkerResult(status="retry", job_id=claim.job_id)

    async def _handle_decision_failure(
        self,
        decision_input: ReconciliationDecisionInput,
        *,
        error_code: str,
        original_error: Exception,
    ) -> WorkerResult:
        claim = decision_input.claim
        if claim.retry_count < len(RETRY_DELAYS):
            return await self._schedule_retry(
                claim,
                error_code=error_code,
                original_error=original_error,
            )
        fallback = decision_input.context.fallback_store(str(decision_input.candidate["summary"]))
        return await self._record_or_apply(decision_input, fallback, fallback=True)

    async def run_once(self) -> WorkerResult:
        claim = await self._repository.claim_pending(
            user_id=self._user_id,
            agent_id=self._agent_id,
            lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return WorkerResult(status="idle")

        try:
            try:
                decision_input = await self._repository.load_decision_input(claim)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return await self._schedule_retry(
                    claim,
                    error_code="decision_input_load_failed",
                    original_error=exc,
                )
            if not decision_input.candidates:
                fallback = decision_input.context.fallback_store(
                    str(decision_input.candidate["summary"])
                )
                return await self._record_or_apply(
                    decision_input, fallback, fallback=True,
                )
            try:
                output = await self._decision_provider(decision_input)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return await self._handle_decision_failure(
                    decision_input,
                    error_code="decision_provider_failed",
                    original_error=error,
                )
            try:
                decisions = parse_decision_batch(output, {claim.job_id: decision_input.context})
                if len(decisions) != 1:
                    raise DecisionBatchError("worker expects one decision per claimed job")
            except DecisionBatchError as error:
                return await self._handle_decision_failure(
                    decision_input,
                    error_code="decision_parse_failed",
                    original_error=error,
                )
            return await self._record_or_apply(decision_input, decisions[0], fallback=False)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._repository.release_lease(claim.job_id, claim.lease_token))
            except BaseException:
                logger.error(
                    "memory.reconciliation_worker.release_lease_failed",
                    extra={"job_id": claim.job_id},
                )
            raise


def _normalize_workers(
    workers: ReconciliationWorker | Iterable[ReconciliationWorker],
) -> list[ReconciliationWorker]:
    if isinstance(workers, ReconciliationWorker):
        return [workers]
    return list(workers)


async def run_forever(
    workers: ReconciliationWorker | Sequence[ReconciliationWorker],
    *,
    interval: float | None = None,
) -> None:
    """轮询驱动 run_once 的常驻循环（由调用方 create_task 启动，本函数不自行启动）。

    - 每个 tick 依次清空各 scope worker 的待处理 job；有产出时连续清空不睡眠，
      全部 idle 才睡 interval（可经 MEMORY_RECONCILIATION_WORKER_INTERVAL 配置）
    - 意外异常按指数退避重试（封顶 MAX_POLL_BACKOFF），循环本身永不因单次失败退出；
      CancelledError 直接传播——run_once 内部已 shield 释放租约，取消安全
    """
    seq = _normalize_workers(workers)
    delay = DEFAULT_POLL_INTERVAL if interval is None else max(0.0, float(interval))
    error_backoff = max(delay, MIN_ERROR_BACKOFF)
    while True:
        try:
            any_progress = False
            for worker in seq:
                while True:
                    result = await worker.run_once()
                    if result.status == "idle":
                        break
                    any_progress = True
                    await asyncio.sleep(0)
            error_backoff = max(delay, MIN_ERROR_BACKOFF)
            if not any_progress:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "memory.reconciliation_worker.loop_error",
                extra={"error": str(exc), "backoff": error_backoff},
            )
            await asyncio.sleep(error_backoff)
            error_backoff = min(error_backoff * 2, MAX_POLL_BACKOFF)
