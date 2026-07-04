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
import time
from typing import Any, TYPE_CHECKING

from loguru import logger

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


def _spawn(coro: Any) -> None:
    """创建 fire-and-forget 后台任务，自动从 _bg_tasks 中移除已完成的任务。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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

    def start_background_task(self, coro: Any) -> None:
        """启动一个 fire-and-forget 后台任务。"""
        _spawn(coro)

    def run_background_tasks(
        self,
        user_input: str,
        reply: str,
        user_id: str,
        source: str,
        emotion: dict,
        tool_results: list,
        session_id: str = "",
    ) -> None:
        """启动所有后台任务（fire-and-forget）。"""
        _spawn(
            self._background_tasks(
                user_input, reply, user_id, source, emotion, tool_results,
                session_id=session_id,
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
    ) -> None:
        await self._run_persistence_tasks(user_input, reply, user_id, source, emotion, session_id)
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
    ) -> None:
        """对话日志、会话更新、记忆编码等持久化任务。

        优化：insert_conversation_log 与 update_session 合并为单次 commit，
        减少 aiosqlite 线程切换次数（Windows SelectorEventLoop 下每次 commit ~30ms）。
        try_idle_encode 涉及向量存储，不纳入批量提交。
        """
        any_write_ok = False
        # 1. 对话日志（不立即 commit）
        try:
            await self.db.insert_conversation_log(
                user_id=user_id,
                source=source,
                user_message=user_input,
                assistant_reply=reply,
                emotion_label=emotion.get("primary", ""),
                session_id=session_id,
                auto_commit=False,
            )
            any_write_ok = True
        except Exception as e:
            logger.warning("bg.conversation_log_failed", error=str(e))

        # 2. 会话更新（不立即 commit）
        if session_id:
            try:
                await self.db.update_session(session_id, auto_commit=False)
                any_write_ok = True
            except Exception as e:
                logger.warning("bg.session_update_failed", error=str(e))

        # 批量提交：仅在有写入成功时调用一次 commit
        if any_write_ok:
            try:
                await self.db.commit()
            except Exception as e:
                logger.warning("bg.batch_commit_failed", error=str(e))

        # 3. 记忆编码（独立，不纳入批量提交）
        if self.memory and len(self.context.history) >= 4:
            try:
                # 合并压缩暂存区的消息和当前历史，确保被压缩丢弃的消息也能被记忆编码
                pre_compressed = self.context.flush_pre_compressed_buffer()
                exchanges = self.context.get_last_n(6)
                if pre_compressed:
                    # 将压缩前的消息转为 exchanges 格式并前置
                    for msg in pre_compressed[-12:]:  # 最多取最近 12 条
                        if msg.get("role") in ("user", "assistant") and msg.get("content"):
                            exchanges.insert(0, {"role": msg["role"], "content": msg["content"][:500]})
                ctx = {
                    "exchanges": exchanges,
                    "emotion": emotion,
                }
                await self.memory.try_idle_encode(ctx, force=True)
            except Exception as e:
                logger.warning("bg.memory_encode_failed", error=str(e))

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
            _spawn(self.notebook_manager.auto_note_after_message(
                user_input, reply, address_term=self.context.current_address_term))

        # 5. 画像标记脏 + 冷启动
        if self.portrait_manager:
            self.portrait_manager.mark_dirty()

        if self.portrait_manager and len(self.context.history) >= 4:
            _spawn(self._portrait_cold_start())

        # 6. 学习评估
        if self.learning_manager:
            _spawn(
                self.learning_manager.evaluate_after_conversation(user_input, reply, tool_results)
            )

        # 7. 本能提取 + curator
        if self.instinct_manager:
            _spawn(
                self.instinct_manager.extract_instincts(user_input, reply, session_id)
            )
            # 每 10 轮对话运行一次 curator（归档过期 + 合并重复）
            async with self._conv_count_lock:
                self._conversation_count += 1
                should_curate = self._conversation_count % 10 == 0
            if should_curate:
                _spawn(self.instinct_manager.curator_run())

    async def _run_scheduled_tasks(self) -> None:
        """会话归档、梦境归档、缓存预热、记忆蒸馏等定时任务。"""
        # 8. 会话自动归档
        _spawn(self._auto_archive_sessions())

        # 9. 梦境归档（每日一次）
        try:
            if await self._should_run("dream_archive", interval_hours=24):
                _spawn(self._dream_archive_task())
        except Exception as e:
            logger.warning("bg.dream_archive_schedule_failed", error=str(e))

        # 10. 嵌入缓存预热（每 5 分钟）
        try:
            if await self._should_run("warm_embedding_cache", interval_hours=5 / 60):
                _spawn(self._warm_embedding_cache())
        except Exception as e:
            logger.warning("bg.warm_embedding_cache_schedule_failed", error=str(e))

        # 11. 记忆蒸馏压缩（每 6 小时，仅 MEMORY_DISTILL_ENABLED=true 时启用）
        try:
            import config
            if getattr(config, "MEMORY_DISTILL_ENABLED", False):
                if await self._should_run("memory_distill", interval_hours=6):
                    _spawn(self._distill_memories_task())
        except Exception as e:
            logger.warning("bg.memory_distill_schedule_failed", error=str(e))

        # 12. 经验晋升（每 30 分钟, recurrence≥3 的学习自动晋升到 system prompt）
        #     修复旁路触发: 此前 auto_promote 只在 nudge_engine 情绪引擎里调用,
        #     用户没触发情绪关键词时经验晋升就不发生
        try:
            if self.learning_manager and await self._should_run("learning_promote", interval_hours=0.5):
                from core.preference_pipeline import get_preference_pipeline
                _spawn(get_preference_pipeline().check_promotion(self.learning_manager))
        except Exception as e:
            logger.warning("bg.learning_promote_schedule_failed", error=str(e))

        # 13. 邮箱 OAuth token 定期刷新（每 2 小时，防止 access/refresh token 过期）
        try:
            if await self._should_run("mail_token_refresh", interval_hours=2):
                _spawn(self._refresh_mail_token_task())
        except Exception as e:
            logger.warning("bg.mail_token_refresh_schedule_failed", error=str(e))

    async def _auto_archive_sessions(self) -> None:
        try:
            archived = await self.db.auto_archive_stale_sessions(idle_seconds=3600)
            if archived > 0:
                logger.info("session.auto_archived", count=archived)
        except Exception as e:
            logger.warning("session.auto_archive_failed", error=str(e))

    async def _portrait_cold_start(self) -> None:
        try:
            result = await self.portrait_manager.ensure_exists(
                address_term=self.context.current_address_term)
            if result:
                self.context.user_portrait = result
                logger.info("portrait.cold_start_done", length=len(result))
        except Exception as e:
            logger.warning("portrait.cold_start_failed", error=str(e))

    async def _should_run(self, task_name: str, interval_hours: float) -> bool:
        """检查周期任务是否应运行（基于 cron_last_run 表）"""
        try:
            last_run = await self.db.get_cron_last_run(task_name)
            if last_run is None:
                return True
            return (time.time() - last_run) >= interval_hours * 3600
        except Exception:
            logger.warning("bg.should_run_cron_check_failed: {}", exc_info=True)
            return False

    async def _dream_archive_task(self) -> None:
        """梦境整合 — 每日执行4杆框架（Decay/Merge/Strengthen/Evict）"""
        try:
            from core.dream_consolidation import get_dream_consolidator
            if self.memory:
                # ★ F5 修复：调用 consolidate_from_db 执行完整4杆框架
                # （从DB加载记忆，替代操作空字典的 consolidate_db）
                stats = await get_dream_consolidator().consolidate_from_db(self.memory.memory)
                if stats.get("total", 0) > 0:
                    logger.info("dream.consolidate_completed",
                                total=stats.get("total", 0),
                                decayed=stats.get("decayed", 0),
                                merged=stats.get("merged", 0),
                                strengthened=stats.get("strengthened", 0),
                                evicted=stats.get("evicted", 0))
            await self.db.set_cron_last_run("dream_archive")
        except Exception as e:
            logger.warning("dream.archive_failed", error=str(e))

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
        except Exception as e:
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
        except Exception as e:
            logger.warning("bg.memory_distill_task_failed", error=str(e))

    async def _refresh_mail_token_task(self) -> None:
        """定期刷新邮箱 OAuth token — 每 2 小时调用一次 message +list 触发 auto_refresh，
        防止 access/refresh token 因长期不使用而过期。"""
        try:
            from tools.mail_tools import _resolve_agently_cli, _run_agently
            if not _resolve_agently_cli():
                await self.db.set_cron_last_run("mail_token_refresh")
                return
            # 调用 message +list --limit 1 触发 token 自动刷新
            rc, out, err = await _run_agently(
                ["message", "+list", "--dir", "inbox", "--limit", "1"],
                timeout=30,
            )
            if rc == 0:
                logger.info("mail.token_refresh_ok")
            elif rc == 3:
                # invalid_grant: 授权失效，清除缓存让前端状态同步
                logger.warning("mail.token_refresh_failed_invalid_grant")
                try:
                    from web.routers.mail_manage import _clear_auth_status_cache
                    _clear_auth_status_cache()
                except Exception:
                    logger.debug("bg.clear_auth_status_cache_error: {}", exc_info=True)
            else:
                logger.warning("mail.token_refresh_failed", rc=rc, err=err[:200] if err else "")
            await self.db.set_cron_last_run("mail_token_refresh")
        except Exception as e:
            logger.warning("bg.mail_token_refresh_failed", error=str(e))

    @staticmethod
    def get_bg_tasks() -> set[asyncio.Task]:
        """返回当前活跃的后台任务集合（供 shutdown 使用）。"""
        return _bg_tasks

    @staticmethod
    def clear_bg_tasks() -> None:
        """清空后台任务集合。"""
        _bg_tasks.clear()
