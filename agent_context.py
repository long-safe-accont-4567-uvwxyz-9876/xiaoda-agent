import asyncio
import time
from typing import Any, Callable, Optional
from loguru import logger


def estimate_tokens(text: str) -> int:
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en = len(text) - cn
    # 英文系数 0.25 与 context_usage.py 保持一致（之前是 0.5，导致估算偏高）
    return int(cn * 1.5 + en * 0.25)


class AgentContext:

    MAX_HISTORY_TOKENS = 6000
    SYSTEM_PROMPT_TOKENS_BUDGET = 2000
    DYNAMIC_CACHE_TTL = 600
    PORTRAIT_CACHE_TTL = 1800
    COMPRESS_TARGET_RATIO = 0.6   # 压缩目标：60% 的 MAX_HISTORY_TOKENS
    MAX_COMPRESS_ROUNDS = 5        # 最大压缩轮数
    MAX_COMPRESSED_SUMMARY_LEN = 3000

    def __init__(self, system_prompt: str = "", system_prompt_loader: Callable[..., str] | None = None,
                 router: Optional[Any]=None, security_filter: Optional[Any]=None) -> None:
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
        self.klee_context: str | None = None
        self.learned_rules: str | None = None
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
        # 并发安全锁
        self._lock = asyncio.Lock()
        # 子代理 A2A 共享黑板（由 AgentCore 注入，None 时跳过黑板逻辑）
        self.shared_blackboard: Any = None
        # 群聊多用户上下文隔离：按 user_id 缓存各自的 history 和压缩摘要
        # 解决群聊场景下多用户共享单例 context 导致的串话和隐私泄露
        self._user_histories: dict[str, list[dict]] = {}
        self._user_summaries: dict[str, str] = {}
        self._current_user_id: str = ""

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
            # 保存当前用户上下文
            if self._current_user_id:
                self._user_histories[self._current_user_id] = list(self.history)
                self._user_summaries[self._current_user_id] = self._compressed_summary
            # 加载目标用户上下文
            self.history = list(self._user_histories.get(user_id, []))
            self._compressed_summary = self._user_summaries.get(user_id, "")
            self._current_user_id = user_id
            # memory_retrieval 是请求级的，切换用户时清空避免串味
            self.memory_retrieval = None

    async def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        msg = {"role": role, "content": str(content) if content is not None else ""}
        if kwargs.get("reasoning_content"):
            msg["reasoning_content"] = kwargs["reasoning_content"]
        if kwargs.get("tool_calls"):
            msg["tool_calls"] = kwargs["tool_calls"]
        async with self._lock:
            self.history.append(msg)
            self._last_message_time = time.time()
            await self._trim_history()

    async def _trim_history(self) -> None:
        if len(self._compressed_summary) > self.MAX_COMPRESSED_SUMMARY_LEN:
            self._compressed_summary = self._compressed_summary[-self.MAX_COMPRESSED_SUMMARY_LEN:]

        if not self.history or self._history_tokens() <= self.MAX_HISTORY_TOKENS:
            return

        target_tokens = int(self.MAX_HISTORY_TOKENS * self.COMPRESS_TARGET_RATIO)

        # 尝试使用 ContextCompressor 进行更好的压缩
        if self._compressor is None and self._router:
            try:
                from memory.context_compressor import get_context_compressor
                self._compressor = get_context_compressor(router=self._router)
            except Exception:
                self._compressor = None

        # Token 目标驱动的迭代压缩，最多 MAX_COMPRESS_ROUNDS 轮
        for _round in range(self.MAX_COMPRESS_ROUNDS):
            if self._history_tokens() <= target_tokens:
                return

            preserve_count = min(10, len(self.history))
            compressible = self.history[:len(self.history) - preserve_count]

            if not compressible:
                break

            if self._compressor:
                try:
                    result = self._compressor.compress_history(self.history, keep_recent=preserve_count // 2)
                    compressed_msgs = result.messages
                    if len(compressed_msgs) < len(self.history):
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
                        logger.info("context.compressed_with_ccr", round=_round + 1, tokens=self._history_tokens(), target=target_tokens)
                        continue
                except Exception as e:
                    logger.debug("context.ccr_compress_failed", error=str(e))

            # 回退到原有压缩逻辑
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
                logger.info("context.compressed", round=_round + 1, compressed=compress_count, tokens=self._history_tokens(), target=target_tokens)
            else:
                # 摘要失败，强制移除最旧的消息
                removed = self.history.pop(0)
                logger.debug("context.trimmed", role=removed["role"], preview=removed["content"][:40])

        # 最终强制裁剪：如果 5 轮后仍超限，强制移除最旧的消息
        while self.history and self._history_tokens() > self.MAX_HISTORY_TOKENS:
            removed = self.history.pop(0)
            logger.debug("context.force_trimmed", role=removed["role"], preview=removed["content"][:40])

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
            prefix = {"user": "用户", "assistant": "纳西妲", "tool": "工具结果"}.get(role, role)
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
                    "chat_flash",
                    [
                        {"role": "system", "content": "请将以下对话记录压缩为1-2句话的摘要，保留关键信息和上下文。只输出摘要，不要加任何前缀。"},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.3,
                    max_tokens=200,
                ),
                timeout=5.0,
            )
            if isinstance(result, str) and result.strip():
                return result.strip()
            return self._quick_summarize(messages)
        except asyncio.TimeoutError:
            logger.debug("context.summarize_timeout, fallback to quick")
            return self._quick_summarize(messages)
        except Exception as e:
            logger.debug("context.summarize_failed", error=str(e))
            return self._quick_summarize(messages)

    def _quick_summarize(self, messages: list[dict]) -> str:
        lines = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if not content:
                continue
            if role == "tool":
                tool_name = m.get("name", "工具")
                lines.append(f"[{tool_name}]: {content[:60]}")
                continue
            prefix = {"user": "用户", "assistant": "纳西妲"}.get(role, role)
            lines.append(f"{prefix}: {content[:80]}")
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
        """
        from datetime import datetime
        now = datetime.now()
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

        return (f"当前时间：{now.year}年{now.month}月{now.day}日 星期{weekday} "
                f"{hour:02d}:{minute:02d}（{period}）。这是纳西妲真切感受到的此刻，"
                f"是她回应时唯一参照的时间。")

    def _build_dynamic_prompt(self) -> str:
        now = time.time()
        if self._cached_dynamic_prompt and (now - self._dynamic_cache_ts) < self.DYNAMIC_CACHE_TTL:
            return self._cached_dynamic_prompt

        parts = []

        if self._compressed_summary:
            parts.append(f"[已压缩的早期对话摘要（仅供参考，不需要回应。当前用户身份：{self.current_address_term}。根据当前用户意图独立判断是否需要调用工具）]\n{self._compressed_summary}")

        if self._restored_summary:
            parts.append(f"[近期对话摘要（仅供参考，不需要回应。当前用户身份：{self.current_address_term}。根据当前用户意图独立判断是否需要调用工具）]\n{self._restored_summary}")

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

    def build_messages(self, user_input: str) -> list[dict]:
        # === Stable 层：场景感知动态排序 ===
        # 根据用户输入自动调整 MD 模块顺序，让最相关的靠近用户输入
        from prompt_builder import build_scene_aware_prompt
        stable_content = build_scene_aware_prompt(user_input, self.current_address_term)
        if self.instinct_prompt:
            stable_content = stable_content + "\n\n---\n\n" + self.instinct_prompt if stable_content else self.instinct_prompt

        # === Context 层（按项目/用户缓存，偶尔变化）===
        context_parts = []
        dynamic = self._build_dynamic_prompt()
        if dynamic:
            context_parts.append(dynamic)
        context_content = context_parts[0] if context_parts else ""

        # === Volatile 层（每次重建，频繁变化）===
        volatile_parts = []
        volatile_parts.append(self._build_time_context())
        if self.emotion_hint:
            volatile_parts.append(f"[感知到{self.current_address_term}的情绪：{self.emotion_hint}]")
        if self.memory_retrieval:
            mem_texts = []
            for m in self.memory_retrieval[:3]:
                summary = m.get("summary", "")
                if summary:
                    # 注入时间戳，让 LLM 知道每条记忆发生的时间（解决"没有时间戳"问题）
                    ts = m.get("timestamp", 0)
                    if ts:
                        try:
                            _date_str = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
                            mem_texts.append(f"· [{_date_str}] {summary[:100]}")
                        except (ValueError, TypeError, OSError):
                            mem_texts.append(f"· {summary[:100]}")
                    else:
                        mem_texts.append(f"· {summary[:100]}")
                kg_ctx = m.get("kg_context", "")
                if kg_ctx:
                    mem_texts.append(kg_ctx[:200])
            if mem_texts:
                volatile_parts.append("[相关记忆]\n" + "\n".join(mem_texts))
        if self.notebook_focus:
            volatile_parts.append(f"[当前关注点] {self.notebook_focus}")
        if self.pending_tasks:
            task_lines = "\n".join(self.pending_tasks[:5])
            volatile_parts.append(f"[待办提醒]\n{task_lines}")
        if self.klee_context:
            volatile_parts.append(f"[可莉的回应（仅供参考，用自己的话转述，不要直接复制）]\n{self.klee_context}")
        volatile_content = "\n".join(volatile_parts) if volatile_parts else ""

        # 拼接三层
        system_content = stable_content
        if context_content:
            system_content += "\n\n---\n\n" + context_content
        if volatile_content:
            system_content += "\n\n---\n\n" + volatile_content

        messages = [{"role": "system", "content": system_content}]

        for msg in self.history:
            m = {"role": msg["role"], "content": str(msg.get("content", "")) if msg.get("content") is not None else ""}
            if msg.get("tool_calls"):
                m["tool_calls"] = msg["tool_calls"]
            # 注意：reasoning_content 不发送到 API（OpenAI API 不支持此字段）
            # 它仅保存在 history 中供内部使用
            messages.append(m)

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
            # 按 user_id 过滤，limit 从 20 缩减到 10（实际只用了 10 条）
            rows = await db.memory.get_recent_conversations(limit=10, user_id=user_id) if user_id else await db.memory.get_recent_conversations(limit=10)
            if not rows:
                return

            summaries = []
            for row in rows:
                user_msg = row.get("user_message", "")
                asst_msg = row.get("assistant_reply", "")
                if not user_msg and not asst_msg:
                    continue
                user_preview = user_msg[:60].replace("\n", " ") if user_msg else ""
                asst_preview = asst_msg[:60].replace("\n", " ") if asst_msg else ""
                summaries.append(f"· {term}: {user_preview} → 纳西妲: {asst_preview}")

            if summaries:
                self._restored_summary = "\n".join(summaries[-10:])
                logger.info("context.restored", items=len(summaries), user_id=user_id, term=term)
        except Exception as e:
            logger.warning("context.restore_failed", error=str(e))

    def get_nahida_prompt(self) -> str:
        """获取纳西妲的系统提示词。

        依次尝试从 system_prompt 属性、_system_prompt_loader 回调获取，
        均失败时返回默认提示词。用于子 Agent 汇总、工具结果摘要等场景。

        Returns:
            str: 纳西妲的系统提示词文本
        """
        nahida_prompt = getattr(self, "system_prompt", "") or ""
        if not nahida_prompt and hasattr(self, "_system_prompt_loader") and self._system_prompt_loader:
            try:
                nahida_prompt = self._system_prompt_loader(address_term=self.current_address_term)
            except Exception as e:
                logger.warning(f"加载纳西妲系统提示词失败: {e}")
        if not nahida_prompt:
            nahida_prompt = "你是纳西妲，须弥的草神。"
        return nahida_prompt

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
