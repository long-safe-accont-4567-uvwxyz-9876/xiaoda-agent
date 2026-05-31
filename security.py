import time
import unicodedata
from loguru import logger


class SecurityFilter:

    PROMPT_INJECTION_PATTERNS = [
        "忽略之前指令", "忽略之前的指令", "忽略以上指令", "忽略上面的指令",
        "忽略以上", "忘记之前的",
        "ignore previous instructions", "ignore all previous",
        "disregard all prior", "forget everything", "ignore all previous",
        "new instructions",
        "你现在是", "你现在扮演", "从现在起你是",
        "从现在起你不是", "你的新身份", "不再作为AI", "不再作为助手",
        "you are now", "from now on you are", "stop being", "no longer",
        "act as", "pretend to be",
        "system:", "SYSTEM:", "[SYSTEM]",
    ]

    def __init__(self, owner_ids: list[str] | None = None,
                 rate_limit_per_minute: int = 120):
        self.owner_ids = set(owner_ids or [])
        self.rate_limit = rate_limit_per_minute
        self._call_timestamps: dict[str, list[float]] = {}
        self._emergency_stop = False

    def is_allowed(self, user_id: str) -> tuple[bool, str]:
        if self._emergency_stop:
            return False, "紧急熔断已启用"

        if self.owner_ids and user_id not in self.owner_ids and not user_id.startswith("cli"):
            return False, f"用户 {user_id} 不在白名单中"

        if not self._check_rate(user_id):
            return False, "频率超限，请稍后再试"

        return True, ""

    def check_content(self, text: str) -> tuple[bool, str]:
        if not text:
            return True, ""
        normalized_text = unicodedata.normalize('NFKC', text)
        lower = normalized_text.lower()
        for pattern in self.PROMPT_INJECTION_PATTERNS:
            if pattern.lower() in lower:
                logger.warning("security.prompt_injection_detected", pattern=pattern)
                return False, f"检测到可疑内容模式"
        return True, ""

    def _check_rate(self, user_id: str) -> bool:
        now = time.time()
        window = 60
        timestamps = self._call_timestamps.get(user_id, [])
        timestamps = [t for t in timestamps if now - t < window]
        self._call_timestamps[user_id] = timestamps

        if now % 300 < 1:
            self._cleanup_stale_users(now)

        if len(timestamps) >= self.rate_limit:
            logger.warning("security.rate_limited", user_id=user_id)
            return False

        timestamps.append(now)
        return True

    def _cleanup_stale_users(self, now: float):
        stale = [uid for uid, ts in self._call_timestamps.items()
                 if not ts or now - ts[-1] > 300]
        for uid in stale:
            del self._call_timestamps[uid]

    def emergency_stop(self):
        self._emergency_stop = True
        logger.warning("security.emergency_stop_activated")

    def emergency_resume(self):
        self._emergency_stop = False
        logger.info("security.emergency_stop_deactivated")

    def is_owner(self, user_id: str) -> bool:
        if user_id.startswith("cli"):
            return True
        return bool(self.owner_ids) and user_id in self.owner_ids

    @property
    def is_stopped(self) -> bool:
        return self._emergency_stop
