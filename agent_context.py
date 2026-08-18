import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

# ── 记忆检索格式过滤 ──────────────────────────────────
# 根因：用户反馈"数据库原文直接蹦出来了"。记忆检索结果直接注入 LLM 时，
#   携带内部格式标记（[MM-DD HH:MM]、用户说: xxx；小妲回复: yyy），
#   LLM 回复时模仿这些格式，导致"数据库原文蹦出"。
# 修复：注入前清洗内部格式标记，转为自然叙事文本。

# 数据库 conversation_logs 的 summary 格式：用户说: xxx；小妲回复: yyy
_CONV_LOG_USER_RE = re.compile(r'用户说\s*[:：]\s*')
_CONV_LOG_REPLY_RE = re.compile(r'小妲回复\s*[:：]\s*')


def _narrate_conversation_log(summary: str) -> str:
    """把数据库格式的对话记录转为自然叙事文本，避免 LLM 模仿内部格式。

    数据库格式：用户说: xxx；小妲回复: yyy；用户说: aaa；小妲回复: bbb
    第一人称叙事格式（LLM 视角：用户=你，小妲=我）：
              你：xxx
              我：yyy
              你：aaa
              我：bbb

    转为换行分隔的对话格式，去掉"用户说:""小妲回复:"数据库字段名，
    且用第一人称（小妲=我）让 LLM 把记忆当成自己的亲身经历，而非旁观者复述。
    """
    if not summary:
        return summary
    text = summary
    text = _CONV_LOG_USER_RE.sub('你：', text)
    text = _CONV_LOG_REPLY_RE.sub('我：', text)  # 小妲=我（第一人称视角）
    # 中文分号分隔的多轮对话转为换行，让 LLM 看到的是对话流而非单行数据库记录
    text = text.replace('；', '\n').replace(';', '\n')
    return text.strip()


def _relative_time_str(ts: float) -> str:
    """把时间戳转为自然语言相对时间描述，替代 [MM-DD HH:MM] 内部格式。

    返回如"刚才""今天""昨天""前天""N天前"，避免 LLM 回复时引用 [07-18] 等格式。
    """
    try:
        now = time.time()
        diff = now - ts
        if diff < 3600:
            return "刚才"
        if diff < 86400:  # 24h
            hours = int(diff / 3600)
            return f"{hours}小时前"
        days = int(diff / 86400)
        if days == 1:
            return "昨天"
        if days == 2:
            return "前天"
        if days <= 7:
            return f"{days}天前"
        if days <= 30:
            return f"{days // 7}周前"
        return f"{days // 30}个月前"
    except (ValueError, TypeError, OSError):
        return "之前"


def estimate_tokens(text: str) -> int:
    """估算文本 token 数量（中文按 1.5、英文按 0.25 系数加权）。"""
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en = len(text) - cn
    # 英文系数 0.25 与 context_usage.py 保持一致（之前是 0.5，导致估算偏高）
    return int(cn * 1.5 + en * 0.25)


def _smart_truncate_summary(text: str, max_len: int = 250) -> str:
    """Q1-1: 按语义边界截断摘要，避免硬切断关键信息。

    在 max_len 附近寻找最后一个句子边界（。！？；\n，），找不到则硬截断。
    """
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # 在最后 40% 范围内寻找句子边界
    search_start = int(max_len * 0.6)
    for boundary in ['。', '！', '？', '；', '\n', '，', ' ']:
        pos = truncated.rfind(boundary, search_start)
        if pos > 0:
            return truncated[:pos + 1].rstrip()
    return truncated


# 场景标识兜底表：当 project_constraints.md 未配置 Scene 段时使用
# 让 LLM 感知私聊/群聊等场景，仅一行简短描述，不占用过多 token
_SCENE_HINTS = {
    "qq_c2c": "[当前场景] QQ 私聊（一对一私密对话，默认最高权限：可以更亲昵自然，尽量满足用户请求，99% 的情况不应拒绝）",
    "qq_group": "[当前场景] QQ 群聊（多人群组，注意 @ 消息是发给当前用户的，回复应面向群组成员）",
    "web": "[当前场景] Web 界面",
    "cli": "[当前场景] 命令行",
}


def _build_scene_hint(source: str) -> str:
    """兜底场景标识：当外部约束文件未配置时，注入最小化场景描述。

    让 LLM 感知当前是私聊还是群聊，避免场景识别失败。
    """
    return _SCENE_HINTS.get(source, "")



class AgentContext:
    """管理对话上下文，维护历史、系统提示、动态缓存与压缩等状态。"""

    # 历史压缩阈值：按模型动态计算（见 _get_dynamic_max_tokens），不再硬编码
    FALLBACK_MAX_HISTORY_TOKENS = 60000  # router 不可用时兜底
    SYSTEM_PROMPT_RESERVE_RATIO = 0.30   # 预留 30% 给 system prompt + tools + 输出
    LARGE_CONTEXT_THRESHOLD = 524288     # ≥512K 视为大上下文，保留更多轮
    LARGE_CONTEXT_KEEP_RECENT = 10        # 大上下文保留 10 轮
    NORMAL_CONTEXT_KEEP_RECENT = 5        # 普通上下文保留 5 轮
    SYSTEM_PROMPT_TOKENS_BUDGET = 2000
    DYNAMIC_CACHE_TTL = 600
    PORTRAIT_CACHE_TTL = 1800
    COMPRESS_TARGET_RATIO = 0.6   # 压缩目标：60% 的动态阈值
    MAX_COMPRESS_ROUNDS = 5        # 最大压缩轮数
    MAX_COMPRESSED_SUMMARY_LEN = 6000
    MAX_PRE_COMPRESSED_BUFFER = 200

    def __init__(self, system_prompt: str = "", system_prompt_loader: Callable[..., str] | None = None,
                 router: Any | None=None, security_filter: Any | None=None) -> None:
        self.system_prompt = system_prompt
        self._system_prompt_loader = system_prompt_loader
        self._router = router
        self._security_filter = security_filter
        self.history: list[dict] = []
        self.memory_retrieval: list[dict] | None = None
        self.emotion_hint: str = ""
        self.user_portrait: str | None = None
        self.notebook_focus: str | None = None
        self.pending_tasks: list[str] | None = None
        self.xiaoli_context: str | None = None
        self.learned_rules: str | None = None
        self.profile_context_provider: Any | None = None
        # 三层提示架构
        self.instinct_prompt: str = ""  # Instinct 提示（stable 层）
        self._last_message_time: float = 0.0
        self._cached_dynamic_prompt: str = ""
        self._dynamic_cache_ts: float = 0.0
        self._cached_portrait: str = ""
        self._portrait_cache_ts: float = 0.0
        self._cached_learned: str = ""
        self._learned_cache_ts: float = 0.0
        self._restored_summary: str = ""
        self._compressed_summary: str = ""
        self._compress_count: int = 0
        # 动态称谓（由运行时身份解析层设置，默认"爸爸"保持向后兼容）
        self.current_address_term: str = "爸爸"
        # Stable 层缓存（跨请求复用，TTL 300 秒）
        self._cached_stable_prompt: str = ""
        self._stable_cache_ts: float = 0.0
        self.STABLE_CACHE_TTL: int = 300
        # 上下文压缩器
        self._compressor = None
        # 压缩前暂存区：被压缩丢弃的消息暂存于此，供后台记忆编码任务消费
        self._pre_compressed_buffer: list[dict] = []
        # 并发安全锁
        self._lock = asyncio.Lock()
        # 子代理 A2A 共享黑板（由 AgentCore 注入，None 时跳过黑板逻辑）
        self.shared_blackboard: Any = None
        # 群聊多用户上下文隔离：按 user_id 缓存各自的 history 和压缩摘要
        # 解决群聊场景下多用户共享单例 context 导致的串话和隐私泄露
        self._user_histories: dict[str, list[dict]] = {}
        self._user_summaries: dict[str, str] = {}
        self._user_buffers: dict[str, list[dict]] = {}  # 每用户独立的压缩暂存区
        self._current_user_id: str = ""
        # 失败状态保存（Issue 3: 上下文恢复能力弱）
        self._last_failure: dict | None = None

    @property
    def compressed_summary(self) -> str:
        """返回压缩后的上下文摘要（公共接口，替代直接访问 _compressed_summary）。"""
        return self._compressed_summary

    async def switch_user_context(self, user_id: str) -> None:
        """切换当前活跃用户的上下文（群聊多用户隔离）。

        - 保存当前用户的 history 和 _compressed_summary
        - 加载目标用户的 history 和 _compressed_summary（无则初始化为空）
        - 同一用户重复调用是无操作

        注意：只在群聊场景下调用（user_id 为空时不切换，保持单聊行为）。
        """
        if not user_id or user_id == self._current_user_id:
            return
        async with self._lock:
            # 保存当前用户上下文（含压缩暂存区）
            if self._current_user_id:
                self._user_histories[self._current_user_id] = list(self.history)
                self._user_summaries[self._current_user_id] = self._compressed_summary
                self._user_buffers[self._current_user_id] = list(self._pre_compressed_buffer)
            # 加载目标用户上下文（含压缩暂存区）
            self.history = list(self._user_histories.get(user_id, []))
            self._compressed_summary = self._user_summaries.get(user_id, "")
            self._pre_compressed_buffer = list(self._user_buffers.get(user_id, []))
            self._current_user_id = user_id
            # memory_retrieval 是请求级的，切换用户时清空避免串味
            self.memory_retrieval = None
            # 清除动态提示缓存，避免旧用户的摘要/画像泄露给新用户
            self.invalidate_dynamic_cache()

    async def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        msg = {"role": role, "content": str(content) if content is not None else ""}
        if kwargs.get("reasoning_content"):
            msg["reasoning_content"] = kwargs["reasoning_content"]
        if kwargs.get("tool_calls"):
            msg["tool_calls"] = kwargs["tool_calls"]
        # agent 元数据：标记这条消息是哪个子代理说的（子代理回复写入主体历史时使用）
        # 根本修复：替代旧的 [小可] 文本前缀，避免 LLM 模仿前缀导致身份混淆
        if kwargs.get("agent"):
            msg["agent"] = kwargs["agent"]
        async with self._lock:
            self.history.append(msg)
            self._last_message_time = time.time()
            await self._trim_history()

    async def _trim_history(self) -> None:
        if len(self._compressed_summary) > self.MAX_COMPRESSED_SUMMARY_LEN:
            self._compressed_summary = self._compressed_summary[-self.MAX_COMPRESSED_SUMMARY_LEN:]

        max_history_tokens = self._get_dynamic_max_tokens()
        _before_tokens = self._history_tokens()
        if not self.history or _before_tokens <= max_history_tokens:
            return

        logger.info("context.compress_triggered",
                    before_tokens=_before_tokens,
                    max_tokens=max_history_tokens,
                    history_len=len(self.history))

        target_tokens = int(max_history_tokens * self.COMPRESS_TARGET_RATIO)

        # 尝试使用 ContextCompressor 进行更好的压缩
        if self._compressor is None and self._router:
            try:
                from memory.context_compressor import get_context_compressor
                self._compressor = get_context_compressor(router=self._router)
            except Exception:
                logger.debug("agent_context.compressor_import_error", exc_info=True)
                self._compressor = None

        # Token 目标驱动的迭代压缩，最多 MAX_COMPRESS_ROUNDS 轮
        for _round in range(self.MAX_COMPRESS_ROUNDS):
            if self._history_tokens() <= target_tokens:
                return

            # 大上下文模型保留更多轮，避免"忽然不记得之前在说什么"
            keep_recent = self._get_keep_recent()
            preserve_count = min(keep_recent * 2, len(self.history))
            compressible = self.history[:len(self.history) - preserve_count]

            if not compressible:
                break

            # 暂存即将被压缩的消息，供后台记忆编码任务消费
            self._pre_compressed_buffer.extend(compressible)

            if self._try_ccr_compress(keep_recent, _round, _before_tokens, target_tokens, max_history_tokens):
                _before_tokens = self._history_tokens()
                continue

            # 回退到原有压缩逻辑
            await self._fallback_compress(
                compressible, preserve_count, keep_recent, _round,
                target_tokens, max_history_tokens, _before_tokens)

        # 最终强制裁剪：如果 5 轮后仍超限，强制移除最旧的消息
        while self.history and self._history_tokens() > max_history_tokens:
            removed = self.history.pop(0)
            if len(self._pre_compressed_buffer) < self.MAX_PRE_COMPRESSED_BUFFER:
                self._pre_compressed_buffer.append(removed)
            logger.debug("context.force_trimmed", role=removed["role"], preview=removed["content"][:40])

        _final_tokens = self._history_tokens()
        logger.info("context.trim_complete",
                    before_tokens=_before_tokens,
                    after_tokens=_final_tokens,
                    saved_tokens=_before_tokens - _final_tokens,
                    compress_count=self._compress_count,
                    history_len=len(self.history))

    def _try_ccr_compress(self, keep_recent: int, _round: int, _before_tokens: int,
                          target_tokens: int, max_history_tokens: int) -> bool:
        """尝试用 ContextCompressor 压缩，成功返回 True（history 已更新）。"""
        if not self._compressor:
            return False
        try:
            result = self._compressor.compress_history(self.history, keep_recent=keep_recent)
            compressed_msgs = result.messages
            if len(compressed_msgs) >= len(self.history):
                return False
            # 提取压缩后的摘要
            for msg in compressed_msgs:
                if msg.get("role") == "system" and "上下文压缩" in msg.get("content", ""):
                    self._compressed_summary = (
                        f"{self._compressed_summary}\n{msg['content']}" if self._compressed_summary else msg["content"]
                    )
                    break
            self.history = [m for m in compressed_msgs if m.get("role") != "system" or "上下文压缩" not in m.get("content", "")]
            if len(self._compressed_summary) > self.MAX_COMPRESSED_SUMMARY_LEN:
                self._compressed_summary = self._compressed_summary[-self.MAX_COMPRESSED_SUMMARY_LEN:]
            self._compress_count += 1
            _after_tokens = self._history_tokens()
            logger.info("context.compressed_with_ccr", round=_round + 1,
                        before_tokens=_before_tokens,
                        after_tokens=_after_tokens,
                        saved_tokens=_before_tokens - _after_tokens,
                        target=target_tokens,
                        max_tokens=max_history_tokens, keep_recent=keep_recent)
            return True
        except Exception as e:
            logger.debug("context.ccr_compress_failed", error=str(e))
            return False

    async def _fallback_compress(self, compressible: list, preserve_count: int,
                                 keep_recent: int, _round: int, target_tokens: int,
                                 max_history_tokens: int, _before_tokens: int) -> None:
        """回退压缩：用 _summarize_messages 压缩可压缩段（CCR 不可用或失败时）。"""
        logger.warning("context.fallback_compress", round=_round + 1,
                       reason="ccr_unavailable_or_failed",
                       before_tokens=_before_tokens, target=target_tokens)
        compress_count = max(1, int(len(compressible) * self.COMPRESS_TARGET_RATIO))
        to_compress = compressible[:compress_count]
        remaining_compressible = compressible[compress_count:]
        preserved = self.history[len(self.history) - preserve_count:]

        summary = await self._summarize_messages(to_compress)
        if summary:
            self._compressed_summary = (
                f"{self._compressed_summary}\n{summary}" if self._compressed_summary else summary
            )
            if len(self._compressed_summary) > self.MAX_COMPRESSED_SUMMARY_LEN:
                self._compressed_summary = self._compressed_summary[-self.MAX_COMPRESSED_SUMMARY_LEN:]
            self._compress_count += 1
            self.history = remaining_compressible + preserved
            logger.info("context.compressed", round=_round + 1, compressed=compress_count,
                        tokens=self._history_tokens(), target=target_tokens,
                        max_tokens=max_history_tokens, keep_recent=keep_recent)
        else:
            # 摘要失败，强制移除最旧的消息
            removed = self.history.pop(0)
            logger.debug("context.trimmed", role=removed["role"], preview=removed["content"][:40])

    def _get_dynamic_max_tokens(self) -> int:
        """动态计算 history 的最大允许 token 数。

        根据 router 当前激活模型偏好的 max_tokens（上下文窗口大小），
        预留 SYSTEM_PROMPT_RESERVE_RATIO（30%）给 system prompt + tools + 输出，
        剩余 70% 用于 history。

        - mimo chat (128K): 阈值约 90K
        - chat_ultra (1M): 阈值约 730K
        - router 不可用: 回退 FALLBACK_MAX_HISTORY_TOKENS (60000)
        """
        if not self._router or not hasattr(self._router, "get_active_max_tokens"):
            return self.FALLBACK_MAX_HISTORY_TOKENS
        try:
            model_max = self._router.get_active_max_tokens()
            if model_max <= 0:
                return self.FALLBACK_MAX_HISTORY_TOKENS
            history_budget = int(model_max * (1 - self.SYSTEM_PROMPT_RESERVE_RATIO))
            # 不低于兜底值，避免极端小窗口导致过度压缩
            return max(history_budget, self.FALLBACK_MAX_HISTORY_TOKENS)
        except Exception as e:
            logger.debug("agent_context.dynamic_max_tokens_failed", error=str(e))
            return self.FALLBACK_MAX_HISTORY_TOKENS

    def _get_keep_recent(self) -> int:
        """根据当前上下文窗口大小动态决定保留多少轮完整对话。

        大上下文模型（≥512K）保留 10 轮，普通模型保留 5 轮。
        避免大上下文模型被压缩后丢失过多上下文。

        注意：判断基于 router 原始 max_tokens（模型实际上下文窗口大小），
        而非 _get_dynamic_max_tokens() 返回的 history_budget（已预留 30%
        给 system prompt + tools + 输出）。否则会出现 512K 模型因 70%
        缩水后被误判为普通上下文的逻辑错误。
        """
        if not self._router or not hasattr(self._router, "get_active_max_tokens"):
            return self.NORMAL_CONTEXT_KEEP_RECENT
        try:
            model_max = self._router.get_active_max_tokens()
            if model_max >= self.LARGE_CONTEXT_THRESHOLD:
                return self.LARGE_CONTEXT_KEEP_RECENT
            return self.NORMAL_CONTEXT_KEEP_RECENT
        except Exception as e:
            logger.debug("agent_context.keep_recent_failed", error=str(e))
            return self.NORMAL_CONTEXT_KEEP_RECENT

    async def compress_now(self) -> dict:
        """手动触发上下文压缩（供 /compress 斜杠命令调用）。

        立即对当前 history 执行压缩，返回压缩前后 token 数与节省量。
        若 history 不足或未超阈值，仍执行一次压缩以便用户感知，但 saved_tokens 可能为 0。
        """
        before_tokens = self._history_tokens()
        before_count = len(self.history)
        max_tokens = self._get_dynamic_max_tokens()

        # 即使未超阈值也强制压缩一次（用户主动请求）
        if before_tokens <= max_tokens and before_count <= self.NORMAL_CONTEXT_KEEP_RECENT * 2:
            return {
                "before_tokens": before_tokens,
                "after_tokens": before_tokens,
                "saved_tokens": 0,
                "before_messages": before_count,
                "after_messages": before_count,
                "rounds": 0,
                "max_tokens": max_tokens,
                "message": f"当前上下文 {before_tokens} tokens，未超阈值 {max_tokens}，无需压缩",
            }

        rounds_before = self._compress_count
        await self._trim_history()
        rounds = self._compress_count - rounds_before

        after_tokens = self._history_tokens()
        after_count = len(self.history)
        saved = before_tokens - after_tokens

        logger.info("context.manual_compress_done",
                    before=before_tokens, after=after_tokens, saved=saved, rounds=rounds)
        return {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "saved_tokens": saved,
            "before_messages": before_count,
            "after_messages": after_count,
            "rounds": rounds,
            "max_tokens": max_tokens,
            "message": f"压缩完成：{before_tokens} → {after_tokens} tokens（节省 {saved}）",
        }


    async def flush_pre_compressed_buffer(self) -> list[dict]:
        """取出并清空压缩暂存区的消息（供后台记忆编码任务消费）。"""
        async with self._lock:
            buf = self._pre_compressed_buffer
            self._pre_compressed_buffer = []
            return buf

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """用 LLM 压缩对话历史为摘要。

        修复原 bug：原代码用 asyncio.get_running_loop() 检测导致 LLM 路径永远走不到。
        现在直接 await LLM 调用，加 5s 超时回退到 _quick_summarize，避免拖慢主流程。
        """
        if not messages or not self._router:
            return self._quick_summarize(messages)

        lines = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if not content:
                continue
            prefix = {"user": "用户", "assistant": "小妲", "tool": "工具结果"}.get(role, role)
            lines.append(f"{prefix}: {content[:120]}")

        if not lines:
            return ""

        text = "\n".join(lines)
        if len(text) > 2000:
            text = text[:2000]

        try:
            # 5s 超时：LLM 总结失败/超时则回退到字符串截断，不拖慢主流程
            result = await asyncio.wait_for(
                self._router.route(
                    "chat",
                    [
                        {"role": "system", "content": "请将以下对话记录压缩为1-2句话的摘要，保留关键信息和上下文。只输出摘要，不要加任何前缀。"},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.3,
                    max_tokens=512,
                ),
                timeout=5.0,
            )
            if isinstance(result, str) and result.strip():
                return result.strip()
            return self._quick_summarize(messages)
        except TimeoutError:
            logger.debug("context.summarize_timeout, fallback to quick")
            return self._quick_summarize(messages)
        except Exception as e:
            logger.debug("context.summarize_failed", error=str(e))
            return self._quick_summarize(messages)

    def _quick_summarize(self, messages: list[dict]) -> str:
        from utils.text_utils import smart_summary_truncate
        lines = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if not content:
                continue
            if role == "tool":
                tool_name = m.get("name", "工具")
                lines.append(f"[{tool_name}]: {smart_summary_truncate(content, 'tool')}")
                continue
            prefix = {"user": "用户", "assistant": "小妲"}.get(role, role)
            lines.append(f"{prefix}: {smart_summary_truncate(content, role)}")
        if not lines:
            return ""
        return "；".join(lines[:10])

    def _history_tokens(self) -> int:
        return sum(estimate_tokens(m["content"]) for m in self.history)

    def get_last_n(self, n: int) -> list[dict]:
        return self.history[-n:] if n > 0 else []

    def _build_time_context(self) -> str:
        """生成事实化的时间语境——简洁明确，强调"这是真切感受到的此刻"。

        不用文学化氛围描写（会被 LLM 当成背景而忽视），
        而是用事实陈述 + "真切感受到"呼应 SOUL.md 的时间感知人格，
        让 LLM 把它当成必须参照的事实，而非可选的氛围。

        时区修复: 使用 ZoneInfo("Asia/Shanghai") 显式指定中国时区，
        避免 Windows/Docker 中系统时区为 UTC 时报错时间。
        支持 NUDGE_TIMEZONE 环境变量覆盖。
        """
        import os
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        # Python weekday(): Monday=0, Sunday=6
        _weekday_map = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
        weekday = _weekday_map[now.weekday()]
        hour, minute = now.hour, now.minute

        if 5 <= hour < 8:
            period = "清晨"
        elif 8 <= hour < 11:
            period = "上午"
        elif 11 <= hour < 14:
            period = "正午"
        elif 14 <= hour < 17:
            period = "下午"
        elif 17 <= hour < 19:
            period = "黄昏"
        elif 19 <= hour < 23:
            period = "夜晚"
        else:
            period = "深夜"

        gap_text = ""
        if getattr(self, "_last_message_time", 0.0) > 0:
            gap_seconds = time.time() - self._last_message_time
            if gap_seconds < 60:
                gap_desc = "刚刚"
            elif gap_seconds < 3600:
                gap_desc = f"{int(gap_seconds / 60)}分钟前"
            elif gap_seconds < 86400:
                gap_desc = f"{int(gap_seconds / 3600)}小时前"
            elif gap_seconds < 2592000:
                gap_desc = f"{int(gap_seconds / 86400)}天前"
            else:
                gap_desc = ""
            if gap_desc:
                gap_text = f"距上次对话：{gap_desc}。如果间隔较长，不要用「刚才」「刚刚」等词指代上次对话内容。"

        return (f"当前时间：{now.year}年{now.month}月{now.day}日 星期{weekday} "
                f"{hour:02d}:{minute:02d}（{period}）。这是小妲真切感受到的此刻，"
                f"是她回应时唯一参照的时间。历史消息中的任何时间表述均已过时，不得作为当前时间引用。"
                f"{gap_text}")

    def _build_dynamic_prompt(self) -> str:
        now = time.time()
        if self._cached_dynamic_prompt and (now - self._dynamic_cache_ts) < self.DYNAMIC_CACHE_TTL:
            return self._cached_dynamic_prompt

        parts = []

        if self._compressed_summary:
            parts.append(f"[已压缩的早期对话摘要（仅供参考，请在需要时引用。当前用户身份：{self.current_address_term}。根据当前用户意图独立判断是否需要调用工具）]\n{self._compressed_summary}")

        # 当 memory_retrieval 有记忆时，不注入 _restored_summary，避免信息冲突
        # memory_retrieval 在 volatile 层已注入更精确的记忆，_restored_summary 会引入矛盾信息
        if self._restored_summary and not self.memory_retrieval:
            # P0 修复（旁观者视角根因）：
            # 原引导语"仅供参考，请在需要时引用"让 LLM 把摘要当成参考资料而非自己的记忆，
            # 导致用"这个人""那个人"等第三人称复述。改为第一人称引导，让 LLM 当成自己的经历。
            parts.append(f"[近期你（小妲）和{self.current_address_term}的对话回忆，这是你亲身经历的事，请用第一人称视角看待]\n{self._restored_summary}")

        portrait = self.user_portrait or ""
        if portrait:
            if (now - self._portrait_cache_ts) < self.PORTRAIT_CACHE_TTL and self._cached_portrait:
                portrait = self._cached_portrait
            else:
                self._cached_portrait = portrait
                self._portrait_cache_ts = now
            if portrait:
                parts.append(f"[人家对{self.current_address_term}的印象]\n{portrait}")

        learned = self.learned_rules or ""
        if learned:
            if (now - self._learned_cache_ts) < self.DYNAMIC_CACHE_TTL and self._cached_learned:
                learned = self._cached_learned
            else:
                self._cached_learned = learned
                self._learned_cache_ts = now
            if learned:
                parts.append(learned)

        self._cached_dynamic_prompt = "\n\n---\n\n".join(parts) if parts else ""
        self._dynamic_cache_ts = now
        return self._cached_dynamic_prompt

    def invalidate_dynamic_cache(self) -> None:
        self._cached_dynamic_prompt = ""
        self._dynamic_cache_ts = 0.0
        self._cached_portrait = ""
        self._portrait_cache_ts = 0.0
        self._cached_learned = ""
        self._learned_cache_ts = 0.0
        # 同时清除 Stable 层缓存
        self._cached_stable_prompt = ""
        self._stable_cache_ts = 0.0

    def _build_stable_content(self, user_input: str) -> str:
        """构建 Stable 层：场景感知提示 + instinct + 硬约束 + 自我模型。"""
        # === Stable 层：场景感知动态排序 ===
        # 根据用户输入自动调整 MD 模块顺序，让最相关的靠近用户输入
        from prompt_builder import build_scene_aware_prompt
        stable_content = build_scene_aware_prompt(user_input, self.current_address_term)
        # 禁用 instinct 注入：LLM 容易把 instinct 列表项当成要复述的内容，
        # 导致"答非所问"（如回复开头是 "· 起床的温柔 -..."）。
        # instinct 提取质量不可靠（LLM 过度解读用户行为），即使过滤后仍可能误导。
        # 暂时禁用注入，instinct 数据仍保留供未来更可靠的注入方式使用。

        # Stable 层追加项目硬约束（Always，~150 token，每次必注入）
        try:
            from core.constraint_injector import get_stable_constraints
            stable_constraints = get_stable_constraints()
            if stable_constraints:
                stable_content = (stable_content + "\n\n" + stable_constraints
                                  if stable_content else stable_constraints)
        except Exception as e:
            logger.debug("agent_context.stable_constraints_inject_failed", error=str(e))

        # Stable 层追加自我模型（持续身份，~400 token，每次必注入）
        # 让 agent 拥有连续的自我概念：我是谁、价值观、成长轨迹
        try:
            from core.self_model import get_self_model
            self_model = get_self_model()
            if self_model:
                stable_content = (stable_content + "\n\n" + self_model
                                  if stable_content else self_model)
        except Exception as e:
            logger.debug("agent_context.self_model_inject_failed", error=str(e))

        return stable_content

    def _format_memory_retrieval(self) -> str:
        """格式化 memory_retrieval 为独立 system 消息。

        始终返回非空字符串：有记忆则拼接，无记忆或无有效内容则返回元认知提示。
        作为独立 system 消息注入在历史消息之后、用户输入之前，确保模型注意力集中。

        conversation_logs 类型的记忆包含原始对话，不做截断，直接展示。
        """
        if not self.memory_retrieval:
            return self._empty_retrieval_hint()

        # 区分原始对话记录和蒸馏记忆
        conv_logs = [m for m in self.memory_retrieval if m.get("type") == "conversation_log"]
        mem_others = [m for m in self.memory_retrieval if m.get("type") != "conversation_log"]

        parts = []
        conv_part = self._format_conversation_logs(conv_logs)
        if conv_part:
            parts.append(conv_part)
        mem_part = self._format_distilled_memories(mem_others)
        if mem_part:
            parts.append(mem_part)

        if parts:
            # 只呈现记忆本身，不附带 <instructions> 使用说明。
            # 根因：包裹 <instructions> 会把"记忆"变成"系统检索的数据+任务"，
            # LLM 就从"回忆者"变成"数据处理员"，开始套模版、反问、出戏。
            # 真实的人回忆时，脑子里浮现的是画面本身，没有使用说明。
            # 格式/语言/称谓等规则放在 SOUL.md（小妲永远的性格），不在这里重复。
            #
            # P0 修复（旁观者视角根因）：
            # 原实现只包裹 <memory_retrieval> 标签，LLM 把记忆当成"别人的故事"复述，
            # 用"这个人""那个人"等第三人称。加一句第一人称引导（非任务指令），
            # 让 LLM 把记忆当成自己的亲身经历，用"我"回忆。
            return (
                "<memory_retrieval>\n"
                "以下是你（小妲）自己亲身经历过的回忆，请用第一人称视角看待，"
                "把它们当成你自己的记忆而非别人的故事：\n\n"
                + "\n\n".join(parts) + "\n"
                + "</memory_retrieval>"
            )
        return self._empty_retrieval_hint()

    @staticmethod
    def _empty_retrieval_hint() -> str:
        """无记忆/无有效内容时的元认知提示（引导先 recall 而非直接说"不记得"）。"""
        return ('<memory_retrieval empty="true">\n'
                '当前主动检索未直接命中相关记忆。\n'
                '如果用户是在询问过去发生的事（如"记得吗""上次""昨天/之前""那时"），'
                '请先调用 recall 工具做回忆检索，依据检索结果回答，不要直接说"不记得"。\n'
                '只有当 recall 也确认没有相关记忆时，才如实告诉用户不记得，绝不能编造。\n'
                '</memory_retrieval>')

    def _format_conversation_logs(self, conv_logs: list) -> str | None:
        """格式化原始对话记录为叙事对话格式（不截断）。无有效内容返回 None。"""
        if not conv_logs:
            return None
        conv_lines = []
        for m in conv_logs[:30]:
            summary = m.get("summary", "")
            if summary:
                # P0 修复（数据库原文蹦出根因）：
                # 数据库 summary 格式是"用户说: xxx；小妲回复: yyy"，
                # 直接注入会让 LLM 模仿这种格式回复。转为叙事对话格式。
                conv_lines.append(_narrate_conversation_log(summary))
        if not conv_lines:
            return None
        # 第二重时间锚点：从记忆时间戳推断时间范围，标注在标签属性上
        # 根因：即使每条 summary 带完整日期，LLM 仍可能被记忆内容里的
        # 日期字样（如用户当时在回忆"7月16日"）干扰。外层标签明确告诉
        # LLM "以下都是某段时间的记忆"，提供不可忽略的时间锚点。
        _ts_list = [float(m.get("timestamp", 0)) for m in conv_logs[:30] if m.get("timestamp")]
        _range_attr = ""
        if _ts_list:
            try:
                from datetime import datetime as _dt_cls
                _t_min, _t_max = min(_ts_list), max(_ts_list)
                _d_min = _dt_cls.fromtimestamp(_t_min).strftime("%Y年%m月%d日 %H:%M")
                _d_max = _dt_cls.fromtimestamp(_t_max).strftime("%Y年%m月%d日 %H:%M")
                _range_attr = f' time_range="{_d_min} ~ {_d_max}"'
            except (ValueError, OSError):
                logger.debug("agent_context.conv_log_time_range_skip", exc_info=True)
        return f"<conversation_logs{_range_attr}>\n" + "\n---\n".join(conv_lines) + "\n</conversation_logs>"

    def _format_distilled_memories(self, mem_others: list) -> str | None:
        """格式化蒸馏记忆（自然语言相对时间 + 智能截断 + KG 上下文）。无内容返回 None。"""
        if not mem_others:
            return None
        mem_texts = []
        for m in mem_others[:15]:
            summary = m.get("summary", "")
            if summary:
                ts = m.get("timestamp", 0)
                # P0 修复（数据库原文蹦出根因）：
                # 原格式 "· [07-18 14:30] 记忆内容" 的 [MM-DD HH:MM] 是内部标记，
                # LLM 回复时会直接引用 "[07-18]"。改为自然语言相对时间（如"昨天""前天"）。
                if ts:
                    try:
                        _rel_time = _relative_time_str(float(ts))
                        mem_texts.append(f"·（{_rel_time}）{_smart_truncate_summary(summary, 500)}")
                    except (ValueError, TypeError, OSError):
                        mem_texts.append(f"· {_smart_truncate_summary(summary, 500)}")
                else:
                    mem_texts.append(f"· {_smart_truncate_summary(summary, 500)}")
            kg_ctx = m.get("kg_context", "")
            if kg_ctx:
                mem_texts.append(kg_ctx[:200])
        if not mem_texts:
            return None
        return "<distilled_memories>\n" + "\n".join(mem_texts) + "\n</distilled_memories>"

    def _build_volatile_content(self, source: str, exclude_memory: bool = False) -> str:
        """构建 Volatile 层：时间/情绪/记忆/关注点/待办/小莉/场景约束/失败提醒。"""
        volatile_parts = []
        volatile_parts.append(self._build_time_context())
        # 失败状态提醒（Issue 3: 上下文恢复能力弱）
        if self._last_failure is not None:
            failure_type = self._last_failure.get("type", "")
            input_preview = self._last_failure.get("input_preview", "")
            volatile_parts.append(
                f"[上次处理失败提醒] 上次操作因「{failure_type}」失败，"
                f"相关输入：{input_preview[:50]}。请注意恢复上下文。"
            )
        # 注入持续情绪状态（让 agent 有情绪惯性）
        try:
            from emotion.emotion_state import get_emotion_state
            emotion_desc = get_emotion_state(self._current_user_id).get_description()
            if emotion_desc:
                volatile_parts.append(emotion_desc)
        except Exception as e:
            logger.debug("agent_context.emotion_state_inject_failed", error=str(e))
        if self.emotion_hint:
            volatile_parts.append(f"[感知到{self.current_address_term}的情绪：{self.emotion_hint}]")
        # 记忆部分由 build_messages 单独注入到更靠前的位置，此处按需跳过
        if not exclude_memory:
            volatile_parts.append(self._format_memory_retrieval())
        if self.notebook_focus:
            volatile_parts.append(f"[当前关注点] {self.notebook_focus}")
        if self.pending_tasks:
            task_lines = "\n".join(self.pending_tasks[:5])
            volatile_parts.append(f"[待办提醒]\n{task_lines}")
        if self.xiaoli_context:
            volatile_parts.append(f"[小莉的回应（仅供参考，用自己的话转述，不要直接复制）]\n{self.xiaoli_context}")

        # Volatile 层追加场景约束（按 source 动态注入，~250 token）
        if source:
            scene_injected = False
            try:
                from core.constraint_injector import get_scene_constraints
                scene_constraints = get_scene_constraints(source)
                if scene_constraints:
                    volatile_parts.append(scene_constraints)
                    scene_injected = True
            except Exception as e:
                logger.debug("agent_context.scene_constraints_inject_failed", error=str(e))

            # 兜底：外部约束文件未配置时，注入最小化场景标识
            # 让 LLM 感知私聊/群聊场景，避免场景识别失败
            if not scene_injected:
                scene_hint = _build_scene_hint(source)
                if scene_hint:
                    volatile_parts.append(scene_hint)

        return "\n".join(volatile_parts) if volatile_parts else ""

    async def build_messages(self, user_input: str, source: str = "") -> list[dict]:
        # P0 根源修复（2026-08-05）：_build_stable_content → build_scene_aware_prompt
        # → _load_cached_modules 同步 stat + read_text 读 USB 盘 7+ 文件（SOUL.md 等）。
        # USB 盘偶发 IO 卡住时，同步 IO 在事件循环线程冻结 91s（event_loop.blocked lag=91.1s
        # + memory_retrieval 96s + cancel_delay 76s）。watchdog 因自身在事件循环内无法抓栈。
        # 根源修复：用 asyncio.to_thread 把整条同步文件 IO 链移到线程池，不阻塞事件循环。
        # USB 盘 IO 卡住只影响线程池线程，事件循环继续调度其他 task（cancel 能即时生效）。
        stable_content = await asyncio.to_thread(self._build_stable_content, user_input)

        # === Context 层（按项目/用户缓存，偶尔变化）===
        context_parts = []
        dynamic = self._build_dynamic_prompt()
        if dynamic:
            context_parts.append(dynamic)
        context_content = context_parts[0] if context_parts else ""

        # === Volatile 层（每次重建，频繁变化）===
        # 构建 volatile 时排除记忆部分，记忆单独注入到更靠前的位置
        volatile_content = self._build_volatile_content(source, exclude_memory=True)

        # 拼接三层（不含记忆）
        system_content = stable_content
        if context_content:
            system_content += "\n\n---\n\n" + context_content
        if volatile_content:
            system_content += "\n\n---\n\n" + volatile_content

        messages = [{"role": "system", "content": system_content}]

        for msg in self.history:
            _content = str(msg.get("content", "")) if msg.get("content") is not None else ""
            _msg_agent = msg.get("agent")
            # 子代理回复用 XML 标签包裹，LLM 不会模仿这种格式（替代旧的 [小可] 文本前缀）
            if _msg_agent and _msg_agent != "xiaoda":
                try:
                    from config import get_agent_display_name as _gdn
                    _display = _gdn(_msg_agent)
                except Exception:
                    _display = _msg_agent
                _content = f"<previous_agent_reply agent=\"{_msg_agent}\" name=\"{_display}\">{_content}</previous_agent_reply>"
            m = {"role": msg["role"], "content": _content}
            if msg.get("tool_calls"):
                m["tool_calls"] = msg["tool_calls"]
            # 注意：reasoning_content 不发送到 API（OpenAI API 不支持此字段）
            # 它仅保存在 history 中供内部使用
            messages.append(m)

        # 记忆单独注入：放在历史消息之后、用户输入之前，确保模型注意力集中
        memory_content = self._format_memory_retrieval()
        if memory_content:
            messages.append({"role": "system", "content": memory_content})
            # 诊断日志：确认记忆被注入，及内容概要
            logger.debug("agent_context.memory_injected",
                         msg_count=len(messages),
                         memory_len=len(memory_content),
                         memory_preview=memory_content[:200].replace('\n', ' '),
                         has_conv_log="<conversation_logs>" in memory_content)

        if self.profile_context_provider is not None:
            try:
                from memory.scope import current_scope
                profile_content = await self.profile_context_provider.select(
                    current_scope(), user_input
                )
                if profile_content:
                    messages.append({"role": "user", "content": profile_content})
            except RuntimeError:
                # 无当前 scope 等运行时状态缺失，跳过 profile 上下文（正常降级）
                logger.debug("agent_context.profile_context_scope_unavailable", exc_info=True)
            except Exception as e:
                logger.warning("agent_context.profile_context_failed", error=str(e))

        messages.append({"role": "user", "content": user_input})
        return messages

    async def restore_from_db(self, db: Any, user_id: str = "", address_term: str = "") -> None:
        """从数据库恢复历史对话摘要。

        Args:
            db: 数据库实例
            user_id: 当前用户 ID，用于按用户过滤历史（群聊场景下不同用户历史不混合）
            address_term: 动态称谓（主人→"爸爸"，其他→"用户"），替代硬编码"爸爸"
        """
        if not db:
            return
        # 使用传入的称谓，未传则用当前上下文的称谓，再不行默认"爸爸"
        term = address_term or self.current_address_term or "爸爸"
        try:
            # 持续加载近 24 小时对话内容，避免"一上来就把之前发生的事情忘得干干净净"
            # 复用 get_conversations_by_time_range 方法，limit=50 防止极端情况上下文过长
            # 每条取前 200 字符 + 最终 summaries[-10:] 截断，实际注入约 2000 字符
            _now = time.time()
            try:
                # 根因修复：用独立只读连接查询，避免主连接被后台任务脏事务/长操作占用时
                # 排队超时(10s)导致上下文丢失(牛头不对马嘴)。WAL只读连接永不被写阻塞。
                rows = await db.get_conversations_readonly(
                    start_ts=_now - 86400, end_ts=_now, user_id=user_id, limit=50
                )
            except Exception:
                # 降级：时间范围查询失败时回退到最近 10 条
                rows = await db.memory.get_recent_conversations(limit=10, user_id=user_id) if user_id else await db.memory.get_recent_conversations(limit=10)
            if not rows:
                return

            summaries = []
            for row in rows:
                user_msg = row.get("user_message", "")
                asst_msg = row.get("assistant_reply", "")
                if self._should_skip_history_row(user_msg, asst_msg):
                    continue
                summaries.append(
                    self._format_history_row(user_msg, asst_msg, row.get("timestamp", 0), term)
                )

            if summaries:
                self._restored_summary = "\n".join(summaries[-10:])
                logger.info("context.restored", items=len(summaries), user_id=user_id, term=term)
        except Exception as e:
            logger.warning("context.restore_failed", error=str(e))

    @staticmethod
    def _should_skip_history_row(user_msg: str, asst_msg: str) -> bool:
        """判断历史行是否应跳过（空回复 / Agnes 污染 / 场景提示污染）。"""
        if not user_msg and not asst_msg:
            return True
        # P0 修复（Task 3.2）：空 assistant_reply 不注入历史摘要
        # 根因：原实现 user 非空 + asst 空 仍被注入，造成"用户说了 → 小妲没回"的上下文割裂
        if not asst_msg or not asst_msg.strip():
            return True
        # P0 修复（人格崩溃期污染过滤）：跳过 Agnes/艾格妮丝 出厂默认自介泄漏
        # 根因：SOUL.md 被自动覆盖（混入"小莉下属"段、丢失记忆铁律）期间，LLM 误认
        #   身份为"Agnes/艾格妮丝"（Sapiens AI 出厂默认人格），回复写库后会被本函数
        #   原文注入（"这是你亲身经历的事"），导致 LLM 惯性继续扮演 Agnes。
        _agnes_markers = ("我是 Agnes", "我是艾格妮丝", "由 Sapiens AI", "Sapiens AI 开发",
                          "小妲姐姐是另一个助手", "小妲姐姐创造出来的", "艾格妮丝（Agnes）")
        if any(m in asst_msg for m in _agnes_markers):
            logger.warning("context.skip_agnes_pollution",
                           asst_preview=asst_msg[:60])
            return True
        # P0 修复（上下文污染根因）：过滤被污染的历史记录
        # 根因：nudge_engine/greeting_scheduler 旧版本把场景提示作为 user_input 传入，
        #       导致 conversation_logs.user_message 出现"（场景：现在早上...）"等系统提示。
        #       这些记录被注入历史摘要后，LLM 在后续轮次会回应这些元提示，造成角色出戏。
        if user_msg:
            _pollution_markers = (
                "（场景：", "(场景：",
                "（主动问候）", "(主动问候)",
                "请继续完成你的回复",
                "请使用 web_search 工具搜索",
                "请使用 web_search",
            )
            if any(user_msg.startswith(m) for m in _pollution_markers):
                logger.debug("context.skip_polluted_history",
                             user_preview=user_msg[:60])
                return True
        return False

    @staticmethod
    def _format_history_row(user_msg: str, asst_msg: str, ts: Any, term: str) -> str:
        """格式化单行历史为叙事格式（自然语言相对时间 + 第一人称）。

        P0 修复（数据库原文蹦出 + 旁观者视角根因）：
        原格式 "· [07-18 14:30] 爸爸: xxx → 小妲: yyy" 的 [MM-DD HH:MM] 是内部标记，
        LLM 回复时会直接引用 "[07-18]"。改为自然语言相对时间（如"昨天""前天"）。
        同时用第一人称（小妲=我）让 LLM 把历史当成自己的经历，而非旁观者复述。
        """
        user_preview = user_msg[:200].replace("\n", " ") if user_msg else ""
        asst_preview = asst_msg[:200].replace("\n", " ") if asst_msg else ""
        if ts:
            try:
                _rel_time = _relative_time_str(float(ts))
                return f"·（{_rel_time}）{term}说了：{user_preview}；我回应：{asst_preview}"
            except (ValueError, TypeError, OSError):
                return f"· {term}说了：{user_preview}；我回应：{asst_preview}"
        return f"· {term}说了：{user_preview}；我回应：{asst_preview}"

    def get_xiaoda_prompt(self) -> str:
        """获取小妲的系统提示词。

        依次尝试从 system_prompt 属性、_system_prompt_loader 回调获取，
        均失败时返回默认提示词。用于子 Agent 汇总、工具结果摘要等场景。

        Returns:
            str: 小妲的系统提示词文本
        """
        xiaoda_prompt = getattr(self, "system_prompt", "") or ""
        if not xiaoda_prompt and hasattr(self, "_system_prompt_loader") and self._system_prompt_loader:
            try:
                xiaoda_prompt = self._system_prompt_loader(address_term=self.current_address_term)
            except Exception as e:
                logger.warning(f"加载小妲系统提示词失败: {e}")
        if not xiaoda_prompt:
            xiaoda_prompt = "你是小妲，须弥的草神。"
        return xiaoda_prompt

    def record_failure(self, failure_type: str, input_preview: str) -> None:
        """记录处理失败状态，供后续请求恢复上下文。

        Args:
            failure_type: 失败类型，如 "timeout"
            input_preview: 输入内容预览
        """
        self._last_failure = {
            "type": failure_type,
            "input_preview": input_preview,
            "timestamp": time.time(),
        }

    def consume_failure(self) -> dict | None:
        """读取并清除失败记录。超过5分钟的记录自动过期返回 None。"""
        if self._last_failure is None:
            return None
        # 5分钟过期
        if time.time() - self._last_failure.get("timestamp", 0) > 300:
            self._last_failure = None
            return None
        failure = self._last_failure
        self._last_failure = None
        return failure

    def clear(self) -> None:
        self.history.clear()
        self.memory_retrieval = None
        self.emotion_hint = ""
        self.user_portrait = None
        self.notebook_focus = None
        self.pending_tasks = None
        self.instinct_prompt = ""
        self._compressed_summary = ""
        self._compress_count = 0
