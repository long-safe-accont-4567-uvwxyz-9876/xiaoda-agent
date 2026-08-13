"""后台任务管理器 — 从 agent_core.py 提取的 fire-and-forget 后台协程。

职责：
- 对话日志写入
- 会话更新
- 记忆编码
- 笔记自动提取
- 画像冷启动
- 学习评估
- 本能提取 + curator
- 会话自动归档
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
import time
from typing import Any, TYPE_CHECKING

from loguru import logger

from utils.metrics import metrics
from core.self_wake import SelfWakeManager, WakeTrigger, get_self_wake_manager

if TYPE_CHECKING:
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    from memory.notebook_manager import NotebookManager
    from emotion.portrait_manager import PortraitManager
    from memory.learning_manager import LearningManager
    from instinct_manager import InstinctManager
    from agent_context import AgentContext

# 全局后台任务集合，用于跟踪和清理
_bg_tasks: set[asyncio.Task] = set()
_task_owner_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "background_task_owner", default=None,
)
_request_context_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "background_request_context", default=None,
)


def set_current_request_context(context: dict | None) -> contextvars.Token:
    return _request_context_var.set(context)


def reset_current_request_context(token: contextvars.Token) -> None:
    _request_context_var.reset(token)


def _on_bg_task_done(task: asyncio.Task) -> None:
    """后台任务完成回调: 移除任务引用并记录异常 (防止异常静默丢失)。"""
    _bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("bg.task_failed error={} task={}", str(exc), task.get_name())


def _spawn(coro: Any, timeout: float | None = None,
           owner: BackgroundTaskManager | None = None) -> asyncio.Task | None:
    """创建 fire-and-forget 后台任务，自动从 _bg_tasks 中移除已完成的任务。

    包含耗时监控：任务完成时记录执行时长，超过 30s 发出告警日志。
    包含 loop 保护：同步上下文调用时降级日志而非崩溃。

    超时约定（默认 None，即不超时）：
        调用方必须显式判断协程性质后决定是否传 timeout：
        - 纯网络/纯计算、可安全中断的协程（如 LLM 抽取后只写文件、
          纯 embed 调用）：显式传 timeout=45，防止卡死阻塞事件循环。
        - 涉及 DB 写入（尤其 auto_commit=False 批次）的协程**不传**。
          根因：超时取消只能中断 await 点，若取消落在 commit 之前，
          已 INSERT 但未 COMMIT 的事务会遗留在连接上，被后续任意一次
          无关 commit 意外提交（产生脏数据），或随连接关闭被回滚丢失
          （数据静默消失）。这两种结局都比"任务卡 45s"更难排查。

    历史背景：auto_note_after_message 曾卡 152s，_encode_task 60s 超时但
        实际 86.9s（内部同步操作不响应 CancelledError）。外层超时只能限制
        卡死时间，无法保证业务一致性，故默认改为 None，由调用方按需启用。
        注意：timeout 只能取消协程的 await 点，同步阻塞需通过 to_thread 规避。
    """
    task_name = getattr(coro, '__name__', coro.__class__.__name__)
    start_time = time.time()
    task_owner = owner or _task_owner_var.get()

    async def _wrapped():
        owner_token = _task_owner_var.set(task_owner)
        try:
            if timeout is None:
                await coro
            else:
                await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("bg.task_timeout name={} timeout={:.0f}s",
                           task_name, timeout)
        except Exception as e:
            logger.warning("bg.task_failed name={} error={}", task_name, str(e)[:200])
        finally:
            _task_owner_var.reset(owner_token)
            elapsed = time.time() - start_time
            if elapsed > 30:
                logger.warning("bg.task_slow name={} elapsed={:.1f}s", task_name, elapsed)
            else:
                logger.debug("bg.task_done name={} elapsed={:.1f}s", task_name, elapsed)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("bg.spawn_no_loop: cannot create task without running event loop, "
                     "task={} will be dropped", task_name)
        coro.close()
        return None
    task = loop.create_task(_wrapped())
    _bg_tasks.add(task)
    task.add_done_callback(_on_bg_task_done)
    if task_owner is not None:
        task_owner._owned_tasks.add(task)
        task.add_done_callback(task_owner._owned_tasks.discard)
    return task


class BackgroundTaskManager:
    """管理 AgentCore 的所有后台异步任务。

    接收对子系统的引用，避免 AgentCore 直接持有后台任务逻辑。
    """

    def __init__(
        self,
        db: DatabaseManager,
        context: AgentContext,
        memory: MemoryManager | None = None,
        notebook_manager: NotebookManager | None = None,
        portrait_manager: PortraitManager | None = None,
        learning_manager: LearningManager | None = None,
        instinct_manager: InstinctManager | None = None,
    ) -> None:
        self.db = db
        self.context = context
        self.memory = memory
        self.notebook_manager = notebook_manager
        self.portrait_manager = portrait_manager
        self.learning_manager = learning_manager
        self.instinct_manager = instinct_manager
        self._conversation_count = 0
        self._conv_count_lock = asyncio.Lock()
        self._owned_tasks: set[asyncio.Task] = set()
        # 周期任务并发去重：记录正在运行的 task_name
        # 根因：_should_run 只读 cron_last_run，而 last_run 在任务完成后才写入，
        # 两条消息并发进入调度时都会判定"该运行"，导致梦境归档/记忆蒸馏/
        # 概念边补建重复执行（重复写入 + 额外 LLM 调用 + DB I/O 争抢）
        self._running_scheduled: set[str] = set()
        # ── Self-Wake 集成（借鉴 OpenWorker selfwake.py）──
        # 将部分常驻循环改为事件驱动，减少无谓的 CPU 占用。
        # 不使用 SelfWake 的模块继续走原有 _should_run 逻辑（向后兼容）。
        self._self_wake: SelfWakeManager = get_self_wake_manager()

    def start_background_task(self, coro: Any) -> None:
        """启动一个 fire-and-forget 后台任务。"""
        self._spawn(coro)

    def _spawn(self, coro: Any, timeout: float | None = None) -> None:
        _spawn(coro, timeout=timeout, owner=self)

    def get_owned_tasks(self) -> set[asyncio.Task]:
        return set(self._owned_tasks)

    async def cancel_background_tasks(self) -> None:
        tasks = list(self._owned_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()

    def run_background_tasks(
        self,
        user_input: str,
        reply: str,
        user_id: str,
        source: str,
        emotion: dict,
        tool_results: list,
        session_id: str = "",
        model_used: str = "",
    ) -> None:
        """启动所有后台任务（fire-and-forget）。

        model_used: 本次回复实际使用的 LLM 模型名，透传到 insert_conversation_log，
        便于后续追溯每条对话使用的模型（排查模型输出质量问题/降级链路分析）。
        """
        self._spawn(
            self._background_tasks(
                user_input, reply, user_id, source, emotion, tool_results,
                session_id=session_id, model_used=model_used,
            )
        )

    async def _background_tasks(
        self,
        user_input: str,
        reply: str,
        user_id: str,
        source: str,
        emotion: dict,
        tool_results: list,
        session_id: str = "",
        model_used: str = "",
    ) -> None:
        # 持久化任务独立 fire-and-forget，避免 DB 长事务阻塞其他后台任务启动
        self._spawn(
            self._run_persistence_tasks(
                user_input, reply, user_id, source, emotion, session_id,
                model_used=model_used,
            )
        )
        await self._run_manager_tasks(user_input, reply, tool_results, session_id)
        await self._run_scheduled_tasks()

    async def _run_persistence_tasks(
        self,
        user_input: str,
        reply: str,
        user_id: str,
        source: str,
        emotion: dict,
        session_id: str,
        model_used: str = "",
    ) -> None:
        """对话日志、会话更新、记忆编码等持久化任务。

        优化：insert_conversation_log 与 update_session 合并为单次 commit，
        减少 aiosqlite 线程切换次数（Windows SelectorEventLoop 下每次 commit ~30ms）。
        try_idle_encode 涉及向量存储，不纳入批量提交。
        """
        any_write_ok = False
        request_context_json = json.dumps(
            _request_context_var.get() or {}, ensure_ascii=False,
        )
        # 1. 对话日志（不立即 commit）
        # P0 修复（Task 3.1）：空回复不入库，避免上下文割裂
        # 根因：call_failed 时 reply="" 仍写入 conversation_logs，
        #       agent_context.py:792-801 注入历史时出现"用户说了 → 小妲没回"的割裂
        # 修复：空回复跳过 insert_conversation_log，仍记录到 errors 表便于排查
        _is_reply_empty = not reply or not reply.strip()
        if _is_reply_empty:
            logger.info("bg.skip_empty_reply",
                        user_input_preview=user_input[:80] if user_input else "",
                        source=source, session_id=session_id,
                        model_used=model_used)
            # 空回复不入 conversation_logs，仅记录到 journal 便于排查
            # （errors 表结构不同，避免引入复杂依赖；journal 日志已足够追溯）
        else:
            # 治本修复（2026-08-05）：空 session_id 用 user_id 兜底。
            # 根因：微信 bot 不传 session_id（session_id=""），写入 conversation_logs 后
            #   WebUI 会话列表 WHERE session_id != '' 过滤掉 → 微信聊天记录不显示。
            # 修复：空 session_id 用 user_id 作为会话标识，确保 WebUI 能显示微信会话。
            if not session_id and user_id:
                session_id = user_id
            # P0 修复（greeting 占位符污染根因）：
            # greeting_scheduler 传 user_input="（主动问候）" 占位符，被入库为
            # conversation_logs.user_message。用户浏览历史时看到系统占位符，且
            # 即使 _pollution_markers 过滤了历史注入，DB 仍有脏数据。
            # 修复：入库前把占位符替换为空串，保留 assistant_reply（问候内容）。
            _GREETING_PLACEHOLDERS = ("（主动问候）", "(主动问候)")
            _logged_user_input = "" if user_input in _GREETING_PLACEHOLDERS else user_input
            # 1+2. 对话日志 + 会话更新：同一写事务，串行化防并发脏事务
            # 根因修复：原版 insert(auto_commit=False)+update(auto_commit=False)+commit()
            # 序列与并发的 memory encode 任务共享 aiosqlite 单连接事务状态，互相
            # commit/rollback → 脏事务/数据丢失/卡顿58s（shield(rollback)/readonly_conn
            # 只治标）。改用 db.write_transaction() 用 asyncio.Lock 串行化所有多语句写事务，
            # 从源头杜绝交叉。任一写入成功即 commit；两条均失败时 commit 空事务（无数据，
            # 仅清空事务状态，等价 rollback 且更简单）。未捕获异常由 write_transaction 的
            # finally 自动 shield(rollback)。
            try:
                async with self.db.write_transaction():
                    try:
                        await self.db.insert_conversation_log(
                            user_id=user_id,
                            source=source,
                            user_message=_logged_user_input,
                            assistant_reply=reply,
                            emotion_label=emotion.get("primary", ""),
                            model_used=model_used,
                            session_id=session_id,
                            request_context_json=request_context_json,
                            auto_commit=False,
                        )
                        any_write_ok = True
                    # CodeRabbit 修复：补充 sqlite3.Error（aiosqlite 抛 sqlite3 异常子类：
                    # OperationalError/IntegrityError 等），否则 DB 错误会传播到 write_transaction
                    # 触发回滚、跳过 update_session，丢失原"两条独立、任一成功即提交"语义
                    except (OSError, ValueError, RuntimeError, sqlite3.Error) as e:
                        logger.error("degradation_triggered bg.conversation_log_failed error={}", str(e))
                    # 2. 会话更新（同一事务内，不单独 commit）
                    if session_id:
                        try:
                            await self.db.update_session(session_id, auto_commit=False)
                            any_write_ok = True
                        except (KeyError, OSError, RuntimeError, sqlite3.Error) as e:
                            logger.error("degradation_triggered bg.session_update_failed error={}", str(e))
            except Exception as e:
                # write_transaction 内部已 shield(rollback)，这里仅记录未预期异常
                logger.error("bg.persistence_txn_failed error={}", str(e))

        # 3. 记忆编码（独立，不纳入批量提交，改为 _spawn 避免阻塞持久化任务）
        # 根因：try_idle_encode 涉及 LLM 调用，可能需要几十秒，await 会阻塞整个 _run_persistence_tasks
        # 修复：改为 _spawn（fire-and-forget），与其他后台任务（notebook/instinct）一致
        _hist_len = len(self.context.history) if self.context else 0
        if self.memory and _hist_len >= 4:
            async def _encode_task():
                try:
                    pre_compressed = await self.context.flush_pre_compressed_buffer()
                    exchanges = self.context.get_last_n(6)
                    if pre_compressed:
                        for msg in pre_compressed[-12:]:
                            if msg.get("role") in ("user", "assistant") and msg.get("content"):
                                exchanges.insert(0, {"role": msg["role"], "content": msg["content"][:500]})
                    ctx = {"exchanges": exchanges, "emotion": emotion}
                    logger.info("bg.memory_encode_start", history_len=_hist_len,
                                exchanges=len(exchanges))
                    # 修复 P2 Bug 11: _encode_task 慢任务（曾 134s）
                    # 根因：try_idle_encode 涉及多次 LLM + embed 调用，无整体超时保护
                    # 90s 超时：embed API 慢时各步骤（vec_upsert 15s + concept 15s + children 20s）
                    # 总和可达 50s+，加上 summary/security/cleanup 需要更多余量。
                    # 各步骤已有独立超时保护，90s 整体超时是最后兜底。
                    # （记忆编码是后台任务，不影响主响应）
                    await asyncio.wait_for(
                        self.memory.try_idle_encode(ctx, force=True),
                        timeout=90.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("bg.memory_encode_timeout", hint="记忆编码超时 90s，跳过本次")
                except Exception as e:
                    logger.warning("bg.memory_encode_failed", error=str(e))
            self._spawn(_encode_task(), timeout=120.0)  # 内部已有 90s 超时，外层 120s 兜底
        else:
            logger.debug("bg.memory_encode_skipped", reason="history_too_short",
                         history_len=_hist_len, need=4)

    async def _run_manager_tasks(
        self,
        user_input: str,
        reply: str,
        tool_results: list,
        session_id: str,
    ) -> None:
        """笔记、画像、学习、本能等管理器任务。"""
        # 4. 笔记自动提取
        if self.notebook_manager:
            self._spawn(self.notebook_manager.auto_note_after_message(
                user_input, reply, address_term=self.context.current_address_term))

        # 5. 画像标记脏 + 冷启动
        if self.portrait_manager:
            self.portrait_manager.mark_dirty()

        if self.portrait_manager and len(self.context.history) >= 4:
            self._spawn(self._portrait_cold_start())

        # 6. 学习评估
        if self.learning_manager:
            self._spawn(
                self.learning_manager.evaluate_after_conversation(user_input, reply, tool_results)
            )

        # 7. 本能提取 + curator
        if self.instinct_manager:
            self._spawn(
                self.instinct_manager.extract_instincts(user_input, reply, session_id)
            )
            # 每 10 轮对话运行一次 curator（归档过期 + 合并重复）
            async with self._conv_count_lock:
                self._conversation_count += 1
                should_curate = self._conversation_count % 10 == 0
            if should_curate:
                self._spawn(self.instinct_manager.curator_run())

    def _spawn_scheduled(self, task_name: str, coro: Any) -> None:
        """启动 _should_run 占位过的周期任务，完成后释放占位。

        必须与 _should_run 配对使用：_should_run 返回 True 时已占位，
        这里保证无论成功/失败/超时都会释放，避免任务永久无法再次调度。
        """
        async def _release_after(inner: Any) -> None:
            try:
                await inner
            finally:
                self._running_scheduled.discard(task_name)

        self._spawn(_release_after(coro))

    async def _run_scheduled_tasks(self) -> None:
        """会话归档、梦境归档、缓存预热、记忆蒸馏等定时任务。"""
        # 8. 会话自动归档
        self._spawn(self._auto_archive_sessions())

        # 9. 梦境归档（每日一次）
        try:
            if await self._should_run("dream_archive", interval_hours=24):
                self._spawn_scheduled("dream_archive", self._dream_archive_task())
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("bg.dream_archive_schedule_failed", error=str(e))

        # 10. 嵌入缓存预热（每 5 分钟）
        try:
            if await self._should_run("warm_embedding_cache", interval_hours=5 / 60):
                self._spawn_scheduled("warm_embedding_cache", self._warm_embedding_cache())
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("bg.warm_embedding_cache_schedule_failed", error=str(e))

        # 11. 记忆蒸馏压缩（每 6 小时，仅 MEMORY_DISTILL_ENABLED=true 时启用）
        try:
            import config
            if getattr(config, "MEMORY_DISTILL_ENABLED", False):
                if await self._should_run("memory_distill", interval_hours=6):
                    self._spawn_scheduled("memory_distill", self._distill_memories_task())
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("bg.memory_distill_schedule_failed", error=str(e))

        # 12. 经验晋升（每 30 分钟, recurrence≥3 的学习自动晋升到 system prompt）
        #     修复旁路触发: 此前 auto_promote 只在 nudge_engine 情绪引擎里调用,
        #     用户没触发情绪关键词时经验晋升就不发生
        try:
            if self.learning_manager and await self._should_run("learning_promote", interval_hours=0.5):
                from core.preference_pipeline import get_preference_pipeline
                self._spawn_scheduled(
                    "learning_promote",
                    get_preference_pipeline().check_promotion(self.learning_manager))
        except (ImportError, OSError, RuntimeError) as e:
            # 释放占位：_should_run 已预约但 setup 失败时不释放会导致永久拒绝
            self._running_scheduled.discard("learning_promote")
            logger.warning("bg.learning_promote_schedule_failed", error=str(e))

        # 13. 邮箱 OAuth token 定期刷新（每 2 小时，防止 access/refresh token 过期）
        try:
            if await self._should_run("mail_token_refresh", interval_hours=2):
                self._spawn_scheduled("mail_token_refresh", self._refresh_mail_token_task())
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("bg.mail_token_refresh_schedule_failed", error=str(e))

        # 14. 概念图边补建（每 30 分钟，auto_link 跳过的边由这里补建）
        try:
            if await self._should_run("concept_link_curator", interval_hours=0.5):
                self._spawn_scheduled("concept_link_curator",
                                      self._concept_link_curator_task())
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("bg.concept_link_curator_schedule_failed", error=str(e))

        # ── Self-Wake 检查（借鉴 OpenWorker selfwake.py）──
        # 检查到期唤醒并触发回调，不阻塞主调度流程。
        # 与 _should_run 逻辑并行，不替代原有调度。
        try:
            due_records = self._self_wake.check_due()
            for record in due_records:
                self._spawn(self._self_wake.fire(record.id))
            # 清理已触发的记录，防止长时间运行内存泄漏（7×24 bot 场景）
            self._self_wake.cleanup_fired()
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("bg.selfwake_check_failed", error=str(e))

    async def _auto_archive_sessions(self) -> None:
        try:
            archived = await self.db.auto_archive_stale_sessions(idle_seconds=3600)
            if archived > 0:
                logger.info("session.auto_archived", count=archived)
        except (OSError, RuntimeError) as e:
            logger.warning("session.auto_archive_failed", error=str(e))

    async def _portrait_cold_start(self) -> None:
        try:
            result = await self.portrait_manager.ensure_exists(
                address_term=self.context.current_address_term)
            if result:
                self.context.user_portrait = result
                logger.info("portrait.cold_start_done", length=len(result))
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("portrait.cold_start_failed", error=str(e))

    _consecutive_failures: dict[str, int] = {}

    async def _should_run(self, task_name: str, interval_hours: float) -> bool:
        """检查周期任务是否应运行（基于 cron_last_run 表）。

        返回 True 时会把 task_name 原子加入 _running_scheduled 占位，
        调用方必须通过 _spawn_scheduled() 启动任务以保证占位被释放。
        """
        # 并发去重：已有同名任务在运行则直接跳过，避免重复执行
        if task_name in self._running_scheduled:
            logger.debug("bg.scheduled_task_already_running name={}", task_name)
            return False
        try:
            last_run = await self.db.get_cron_last_run(task_name)
            if last_run is None:
                # 二次检查：await 期间可能已有并发调用占位（与下方超时分支一致）
                if task_name in self._running_scheduled:
                    return False
                self._running_scheduled.add(task_name)
                return True
            result = (time.time() - last_run) >= interval_hours * 3600
            if result:
                self._consecutive_failures.pop(task_name, None)
                # 二次检查：await 期间可能已有并发调用占位
                if task_name in self._running_scheduled:
                    return False
                self._running_scheduled.add(task_name)
            return result
        except (OSError, RuntimeError):
            count = self._consecutive_failures.get(task_name, 0) + 1
            self._consecutive_failures[task_name] = count
            if count >= 5:
                logger.error(f"periodic_task_possibly_dead: task={task_name}, consecutive_failures={count}")
            elif count >= 3:
                logger.warning(f"periodic_task_degraded: task={task_name}, consecutive_failures={count}")
            else:
                logger.warning(f"periodic_task_db_error: task={task_name}")
            return False

    async def _dream_archive_task(self) -> None:
        """梦境整合 — 每日执行4杆框架（Decay/Merge/Strengthen/Evict）"""
        try:
            from core.dream_consolidation import get_dream_consolidator
            if self.memory:
                # ★ F5 修复：调用 consolidate_from_db 执行完整4杆框架
                # （从DB加载记忆，替代操作空字典的 consolidate_db）
                # ★ G7 修复：同时注入 memory_db 给工厂, 让 scheduler 也能调 consolidate_from_db
                stats = await get_dream_consolidator(
                    memory_db=self.memory.memory
                ).consolidate_from_db(self.memory.memory)
                if stats.get("total", 0) > 0:
                    logger.info("dream.consolidate_completed",
                                total=stats.get("total", 0),
                                decayed=stats.get("decayed", 0),
                                merged=stats.get("merged", 0),
                                strengthened=stats.get("strengthened", 0),
                                evicted=stats.get("evicted", 0))
                # DreamEngineV2 6阶段梦境引擎（渐进接线：与 DreamConsolidator 并存，独立容错）
                await self._run_dream_engine_v2()
            await self.db.set_cron_last_run("dream_archive")
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning("dream.archive_failed", error=str(e))

    async def _run_dream_engine_v2(self) -> None:
        """DreamEngineV2 6阶段梦境引擎接线（独立容错，失败不影响主流程）。

        从 DB 加载记忆 + 批量 embedding 填充 CognitiveMemory，然后运行 6 阶段。
        仅在 vector store 可用时执行；embedding 缺失/失败时安全跳过。
        """
        try:
            _vec = getattr(self.memory, "vec", None)
            if _vec is None or not getattr(_vec, "enabled", False):
                return
            from core.dream_engine_v2 import DreamEngineV2, get_cognitive_memory
            import numpy as np
            cog = get_cognitive_memory()
            # 首次加载：从 DB 读记忆并计算 embedding 填充 CognitiveMemory
            if cog.episodic_size() == 0:
                rows = await self.memory.memory.get_all_memories(limit=500)
                contents = [r.get("summary", "") for r in rows if r.get("summary")]
                if contents:
                    vectors = await _vec.embed(contents)
                    for i, content in enumerate(contents):
                        if i >= len(vectors):
                            break
                        await cog.remember(
                            content,
                            np.asarray(vectors[i], dtype=np.float32),
                            emotion_label="",
                            session_id="dream",
                        )
            dream = DreamEngineV2(cognitive_memory=cog)
            stats = await dream.run_cycle()
            logger.info("dream_engine_v2.cycle_done",
                        cycle=stats.get("cycle", 0),
                        nrem_sampled=stats.get("nrem_sampled", 0),
                        insight_communities=stats.get("insight_communities", 0))
        except Exception as e:
            logger.warning("dream_engine_v2.failed", error=str(e))

    async def _warm_embedding_cache(self) -> None:
        """预热嵌入缓存：将最近 30 条情景记忆摘要写入向量缓存，减少查询时 cache miss。"""
        try:
            if not self.memory or not getattr(self.memory, "vec", None):
                return
            recent = await self.memory.memory.get_episodic_recent(limit=30)
            if not recent:
                await self.db.set_cron_last_run("warm_embedding_cache")
                return
            summaries = [r.get("summary", "") for r in recent if r.get("summary")]
            if summaries:
                await self.memory.vec.warm_cache(summaries)
            await self.db.set_cron_last_run("warm_embedding_cache")
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("bg.warm_embedding_cache_failed", error=str(e))

    async def _distill_memories_task(self) -> None:
        """记忆蒸馏压缩 — 将超过阈值的旧记忆蒸馏为摘要，控制上下文长度。"""
        try:
            if not self.memory:
                await self.db.set_cron_last_run("memory_distill")
                return
            distilled = await self.memory.distill_old_memories()
            if distilled > 0:
                logger.info("memory.distill_task_completed", distilled=distilled)
            await self.db.set_cron_last_run("memory_distill")
        except (OSError, RuntimeError) as e:
            logger.warning("bg.memory_distill_task_failed", error=str(e))

    async def _concept_link_curator_task(self) -> None:
        """概念图边补建 curator — 为 auto_link 跳过的节点批量补建 co-occurrence 边。

        当存活概念节点 >200 时，实时 auto_link 会被跳过以避免阻塞事件循环。
        本任务每 30 分钟运行一次，在后台为无边节点补建边关系。

        N10 根因修复（2026-07-25 17:48-17:51 生产事故根因）：
        原 batch_size=30 在 1400+ 存活节点场景下，batch_link_recent 需加载
        全部节点 keys + 顺序写大量边，在 USB 盘上耗时 >45s（日志显示全天
        每次 bg.task_timeout 45s）。这饱和了 USB I/O 带宽，导致同时段 recall
        工具的 aiosqlite 查询被饿死 60s+ 才超时，触发降级雪崩。
        修复：batch_size 30→5（中间值）→ 最终定为 10。单次处理 10 个无边节点，
        配合 max_edges_per_run=60 限制写入量，I/O 占用控制在 ~10s 内，给 recall
        等用户路径留出 DB 带宽。（CodeRabbit #4: 文档与最终值 batch_size=10 对齐）
        """
        try:
            if not self.memory or not getattr(self.memory, "concept_graph", None):
                await self.db.set_cron_last_run("concept_link_curator")
                return
            _curator_t0 = time.time()
            try:
                # 根治（2026-08-05 rework）：收敛到主连接 write_transaction，删除独立写连接。
                # 根因复盘：历史"独立连接"方案用 separate aiosqlite 连接 + ConceptDB 写概念图，
                # 与主连接/instinct 并发写时争抢 SQLite 单写锁 → "database is locked"（14:37:45
                # 实证）。提高 busy_timeout（5s→20s）只是让写等待而非失败，锁竞争仍在（治标）。
                # 根治：所有写统一走主连接 + write_transaction() 的 asyncio.Lock 串行化 → 只剩
                # 一个写者，跨连接写锁无从谈起。batch_size=10 有界 + synchronous=NORMAL 不 fsync，
                # 本批写已毫秒级，不再有历史"30s+ 独占主连接线程"问题（那是 synchronous=FULL 的
                # fsync 导致，已修）。提交由外层 write_transaction 统一负责。
                from db.db_concept import ConceptDB
                async with self.db.write_transaction():
                    _curator_concept_db = ConceptDB(self.db._conn)
                    # N10: batch_size 30→10，配合 max_edges_per_run=60 限制写入量
                    linked = await asyncio.wait_for(
                        _curator_concept_db.batch_link_recent(
                            batch_size=10, auto_commit=False,
                        ),
                        timeout=30.0,
                    )
                    if linked > 0:
                        logger.info("concept_graph.curator_linked", edges=linked)
            except asyncio.TimeoutError:
                # CodeRabbit #5→#A 修正：超时也更新 last_run，30 分钟内不重试，
                # 避免连续超时 → USB I/O 雪崩。未完成节点由下次 curator 拾起。
                logger.warning("concept_graph.curator_timeout",
                               hint="batch_link 30s 超时，30 分钟后下轮重试本批未完成节点")
                metrics.inc("concept_graph.curator_timeout")
            _curator_ms = int((time.time() - _curator_t0) * 1000)
            if _curator_ms > 10000:
                logger.warning(f"concept_graph.curator_slow elapsed_ms={_curator_ms}")
            await self.db.set_cron_last_run("concept_link_curator")
        except (OSError, RuntimeError) as e:
            logger.warning("bg.concept_link_curator_failed", error=str(e))

    async def _refresh_mail_token_task(self) -> None:
        """定期刷新邮箱 OAuth token — 每 2 小时调用一次 message +list 触发 auto_refresh，
        防止 access/refresh token 因长期不使用而过期。"""
        try:
            from tools.mail_tools import _resolve_agently_cli, _run_agently
            if not _resolve_agently_cli():
                await self.db.set_cron_last_run("mail_token_refresh")
                return
            # 修复 P2 Bug 11: _refresh_mail_token_task 慢任务（曾 58s）
            # 根因：agently-cli 启动 + 网络请求可能卡住，原 timeout=30 只覆盖 _run_agently 内部
            # 加外层 45s 超时：超过则放弃本次刷新（下次 cron 会重试）
            try:
                # 调用 message +list --limit 1 触发 token 自动刷新
                rc, _out, err = await asyncio.wait_for(
                    _run_agently(
                        ["message", "+list", "--dir", "inbox", "--limit", "1"],
                        timeout=30,
                    ),
                    timeout=45.0,
                )
            except asyncio.TimeoutError:
                # P1-3: 不调用 set_cron_last_run —— 超时不是"成功完成"，
                # 保留 stale 的 cron_last_run 让 _should_run() 下次 cron 检查时仍判定需要重试，
                # 避免瞬态网络故障导致 OAuth token 失效长达 2 小时。
                logger.warning("mail.token_refresh_timeout", hint="刷新超时 45s，跳过本次，下次 cron 重试")
                return
            if rc == 0:
                logger.info("mail.token_refresh_ok")
            elif rc == 3:
                # invalid_grant: 授权失效，清除缓存让前端状态同步
                logger.warning("mail.token_refresh_failed_invalid_grant")
                try:
                    from web.routers.mail_manage import _clear_auth_status_cache
                    _clear_auth_status_cache()
                except (ImportError, AttributeError):
                    logger.debug("bg.clear_auth_status_cache_error: {}", exc_info=True)
            else:
                logger.warning("mail.token_refresh_failed", rc=rc, err=err[:200] if err else "")
            await self.db.set_cron_last_run("mail_token_refresh")
        except (OSError, RuntimeError, TimeoutError) as e:
            logger.warning("bg.mail_token_refresh_failed", error=str(e))

    @staticmethod
    def get_bg_tasks() -> set[asyncio.Task]:
        """返回当前活跃的后台任务集合（供 shutdown 使用）。"""
        return _bg_tasks

    @staticmethod
    def clear_bg_tasks() -> None:
        """清空后台任务集合，取消所有未完成的 Task。"""
        for task in list(_bg_tasks):
            if not task.done():
                task.cancel()
        _bg_tasks.clear()


# ── 事件循环阻塞 watchdog ──────────────────────────────────
# 根因：日志显示后台任务集体卡 257-265s（_background_tasks/curator_run/
#   extract_instincts/auto_note_after_message 同时报 task_slow elapsed=257.9s），
#   _spawn 的 timeout=45s 无法取消（asyncio.wait_for 只能取消 await 点，
#   同步阻塞不响应 CancelledError）。说明主事件循环被某个同步操作冻结。
# 监控：每 5 秒心跳，若延迟 >10s 则打印所有线程栈定位阻塞点。
_watchdog_task: asyncio.Task | None = None


async def event_loop_watchdog() -> None:
    """事件循环心跳监控：检测同步阻塞并打印线程栈定位根因。

    每 5 秒打个心跳，如果实际延迟超过 10 秒，说明事件循环被同步操作阻塞
    （如 sqlite 同步 commit、CPU 密集计算、C 扩展 GIL）。此时打印所有线程栈，
    帮助定位阻塞点。
    """
    import sys
    import traceback as _tb
    _CHECK_INTERVAL = 5.0
    _BLOCK_THRESHOLD = 10.0  # 延迟超过 10 秒告警
    last = time.time()
    _last_warn = 0.0
    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)
        except asyncio.CancelledError:
            return
        now = time.time()
        lag = now - last - _CHECK_INTERVAL
        last = now
        if lag <= _BLOCK_THRESHOLD:
            continue
        # 限频：同一阻塞事件 5 分钟内只告警一次
        if _last_warn > 0 and now - _last_warn < 300:
            continue
        _last_warn = now
        logger.error(
            "event_loop.blocked lag={:.1f}s threshold={:.0f}s "
            "hint=事件循环被同步操作阻塞，打印线程栈定位根因",
            lag, _BLOCK_THRESHOLD,
        )
        try:
            for tid, frame in sys._current_frames().items():
                stack_lines = _tb.format_stack(frame)
                # 只打印最后 40 行，避免日志爆炸
                tail = stack_lines[-40:] if len(stack_lines) > 40 else stack_lines
                logger.error(
                    "event_loop.blocked_thread tid={} stack_tail=\n{}",
                    tid, ''.join(tail),
                )
        except Exception as e:
            logger.error("event_loop.watchdog_dump_failed error={}", str(e))


def start_event_loop_watchdog() -> None:
    """启动事件循环 watchdog（幂等，重复调用安全）。"""
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("event_loop.watchdog_no_loop")
        return
    _watchdog_task = loop.create_task(event_loop_watchdog())
    _bg_tasks.add(_watchdog_task)
    _watchdog_task.add_done_callback(_bg_tasks.discard)
    logger.info("event_loop.watchdog_started interval=5s threshold=10s")


def stop_event_loop_watchdog() -> None:
    """停止事件循环 watchdog。"""
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = None
