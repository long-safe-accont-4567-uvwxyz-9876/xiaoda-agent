import time
from typing import Callable
from loguru import logger


def estimate_tokens(text: str) -> int:
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en = len(text) - cn
    return int(cn * 1.5 + en * 0.5)


class AgentContext:

    MAX_HISTORY_TOKENS = 4000
    SYSTEM_PROMPT_TOKENS_BUDGET = 2000
    DYNAMIC_CACHE_TTL = 600
    PORTRAIT_CACHE_TTL = 1800

    def __init__(self, system_prompt: str = "", system_prompt_loader: Callable[[], str] | None = None):
        self.system_prompt = system_prompt
        self._system_prompt_loader = system_prompt_loader
        self.history: list[dict] = []
        self.memory_retrieval: list[dict] | None = None
        self.emotion_hint: str = ""
        self.user_portrait: str | None = None
        self.notebook_focus: str | None = None
        self.pending_tasks: list[str] | None = None
        self.klee_context: str | None = None
        self.learned_rules: str | None = None
        self._last_message_time: float = 0.0
        self._cached_dynamic_prompt: str = ""
        self._dynamic_cache_ts: float = 0.0
        self._cached_portrait: str = ""
        self._portrait_cache_ts: float = 0.0
        self._cached_learned: str = ""
        self._learned_cache_ts: float = 0.0
        self._restored_summary: str = ""

    def add_message(self, role: str, content: str, **kwargs):
        msg = {"role": role, "content": content}
        if kwargs.get("reasoning_content"):
            msg["reasoning_content"] = kwargs["reasoning_content"]
        if kwargs.get("tool_calls"):
            msg["tool_calls"] = kwargs["tool_calls"]
        self.history.append(msg)
        self._last_message_time = time.time()
        self._trim_history()

    def _trim_history(self):
        while self.history and self._history_tokens() > self.MAX_HISTORY_TOKENS:
            removed = self.history.pop(0)
            logger.debug("context.trimmed", role=removed["role"], preview=removed["content"][:40])

    def _history_tokens(self) -> int:
        return sum(estimate_tokens(m["content"]) for m in self.history)

    def get_last_n(self, n: int) -> list[dict]:
        return self.history[-n:] if n > 0 else []

    def _build_dynamic_prompt(self) -> str:
        now = time.time()
        if self._cached_dynamic_prompt and (now - self._dynamic_cache_ts) < self.DYNAMIC_CACHE_TTL:
            return self._cached_dynamic_prompt

        parts = []

        if self._restored_summary:
            parts.append(f"[近期对话摘要（仅供参考，不需要回应）]\n{self._restored_summary}")

        portrait = self.user_portrait or ""
        if portrait:
            if (now - self._portrait_cache_ts) < self.PORTRAIT_CACHE_TTL and self._cached_portrait:
                portrait = self._cached_portrait
            else:
                self._cached_portrait = portrait
                self._portrait_cache_ts = now
            if portrait:
                parts.append(f"[人家对爸爸的印象]\n{portrait}")

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

    def invalidate_dynamic_cache(self):
        self._cached_dynamic_prompt = ""
        self._dynamic_cache_ts = 0.0
        self._cached_portrait = ""
        self._portrait_cache_ts = 0.0
        self._cached_learned = ""
        self._learned_cache_ts = 0.0

    def build_messages(self, user_input: str) -> list[dict]:
        system_content = self._system_prompt_loader() if self._system_prompt_loader else self.system_prompt

        dynamic = self._build_dynamic_prompt()
        if dynamic:
            system_content += "\n\n---\n\n" + dynamic

        messages = [{"role": "system", "content": system_content}]

        for msg in self.history:
            messages.append(msg)

        user_block = user_input
        parts = []

        if self.emotion_hint:
            parts.append(f"[感知到爸爸的情绪：{self.emotion_hint}]")

        if self.memory_retrieval:
            mem_texts = []
            for m in self.memory_retrieval[:3]:
                summary = m.get("summary", "")
                if summary:
                    mem_texts.append(f"· {summary[:100]}")
                kg_ctx = m.get("kg_context", "")
                if kg_ctx:
                    mem_texts.append(kg_ctx[:200])
            if mem_texts:
                parts.append("[相关记忆]\n" + "\n".join(mem_texts))

        if self.notebook_focus:
            parts.append(f"[当前关注点] {self.notebook_focus}")

        if self.pending_tasks:
            task_lines = "\n".join(self.pending_tasks[:5])
            parts.append(f"[待办提醒]\n{task_lines}")

        if self.klee_context:
            parts.append(f"[可莉的回应（仅供参考，用自己的话转述，不要直接复制）]\n{self.klee_context}")

        if parts:
            user_block = "\n".join(parts) + "\n---\n" + user_input

        messages.append({"role": "user", "content": user_block})
        return messages

    async def restore_from_db(self, db):
        if not db:
            return
        try:
            rows = await db.memory.get_recent_conversations(limit=20)
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
                summaries.append(f"· 爸爸: {user_preview} → 纳西妲: {asst_preview}")

            if summaries:
                self._restored_summary = "\n".join(summaries[-10:])
                logger.info("context.restored", items=len(summaries))
        except Exception as e:
            logger.warning("context.restore_failed", error=str(e))

    def clear(self):
        self.history.clear()
        self.memory_retrieval = None
        self.emotion_hint = ""
        self.user_portrait = None
        self.notebook_focus = None
        self.pending_tasks = None
