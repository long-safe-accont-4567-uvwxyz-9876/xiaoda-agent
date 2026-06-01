import os
import time
from openai import AsyncOpenAI
from loguru import logger

from db_analytics import AnalyticsDB


MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")
MIMO_PRO_MODEL = os.getenv("MIMO_PRO_MODEL_NAME", "mimo-v2.5-pro")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")

MIMO_PRICING = {
    "standard": {
        "input_per_m": 0.10,
        "cache_hit_per_m": 0.01,
        "output_per_m": 0.20,
    },
    "pro": {
        "input_per_m": 0.20,
        "cache_hit_per_m": 0.02,
        "output_per_m": 0.40,
    },
}

ROUTE_TABLE = {
    "chat": {"model": MIMO_MODEL, "max_tokens": 1500, "client": "mimo"},
    "chat_pro": {"model": MIMO_PRO_MODEL, "max_tokens": 2000, "client": "mimo"},
    "chat_flash": {"model": MIMO_MODEL, "max_tokens": 1000, "client": "mimo"},
    "chat_mimo": {"model": MIMO_MODEL, "max_tokens": 1500, "client": "mimo"},
    "emotion_analysis": {"model": MIMO_MODEL, "max_tokens": 300, "client": "mimo"},
    "tool_result_wrap": {"model": MIMO_MODEL, "max_tokens": 300, "client": "mimo"},
    "memory_encoding": {"model": MIMO_MODEL, "max_tokens": 800, "client": "mimo"},
}

MODEL_PREFERENCES = {
    "mimo": {"label": "MiMo 模式", "desc": "使用小米 MiMo-V2.5 模型"},
    "mimo-pro": {"label": "MiMo Pro 模式", "desc": "使用小米 MiMo-V2.5-Pro 深度思考"},
}


class ModelRouter:

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 api_key_2: str | None = None, db=None):
        self._client = AsyncOpenAI(api_key=MIMO_API_KEY, base_url=MIMO_BASE_URL) if MIMO_API_KEY else None
        self._db = db
        self._model_preference = "mimo"
        self._cost_buffer: list[dict] = []
        self._cost_flush_threshold = 3
        self._last_cache_warning = 0.0
        self._last_reasoning_content: str | None = None

        self._cache_stats = {
            "total_calls": 0,
            "hit_tokens": 0,
            "miss_tokens": 0,
        }

    def set_db(self, db, analytics: AnalyticsDB | None = None):
        self._db = db
        self._analytics = analytics

    def set_model_preference(self, preference: str) -> bool:
        if preference in MODEL_PREFERENCES:
            self._model_preference = preference
            logger.info("router.preference_changed", preference=preference)
            return True
        return False

    def get_model_preference(self) -> str:
        return self._model_preference

    def get_model_preference_label(self) -> str:
        return MODEL_PREFERENCES.get(self._model_preference, {}).get("label", "未知")

    def resolve_task_type(self, base_task: str) -> str:
        if self._model_preference == "mimo-pro":
            return "chat_pro"
        return base_task

    def _calc_cost(self, prompt_tokens: int, completion_tokens: int,
                   cache_hit_tokens: int = 0, cache_miss_tokens: int = 0,
                   model: str = "") -> float:
        cache_miss = cache_miss_tokens if cache_miss_tokens > 0 else (prompt_tokens - cache_hit_tokens)
        if cache_miss < 0:
            cache_miss = prompt_tokens
        pricing = MIMO_PRICING.get("pro") if "pro" in model else MIMO_PRICING.get("standard")
        input_cost = (cache_miss / 1_000_000) * pricing["input_per_m"]
        cache_cost = (cache_hit_tokens / 1_000_000) * pricing["cache_hit_per_m"]
        output_cost = (completion_tokens / 1_000_000) * pricing["output_per_m"]
        return input_cost + cache_cost + output_cost

    async def _record_usage(self, task_type: str, model: str, response,
                             user_openid: str = "", session_id: str = ""):
        try:
            usage = response.usage
            if not usage:
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            cost = self._calc_cost(prompt_tokens, completion_tokens, cache_hit, cache_miss, model)

            record = {
                "user_openid": user_openid,
                "session_id": session_id,
                "model": model,
                "task_type": task_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "cost_usd": cost,
                "created_at": time.time(),
            }

            if self._analytics:
                self._cost_buffer.append(record)
                if len(self._cost_buffer) >= self._cost_flush_threshold:
                    await self._flush_cost_buffer()
            else:
                logger.debug("router.usage_no_db", task=task_type, cost=f"${cost:.6f}")
        except Exception as e:
            logger.warning("router.usage_record_failed", error=str(e))

    async def _flush_cost_buffer(self):
        if not self._cost_buffer or not self._analytics:
            return
        try:
            await self._analytics.batch_insert_api_usage(self._cost_buffer)
            count = len(self._cost_buffer)
            self._cost_buffer.clear()
            logger.debug("router.cost_flushed", count=count)
        except Exception as e:
            logger.warning("router.cost_flush_failed", error=str(e))

    async def flush_costs(self):
        await self._flush_cost_buffer()

    async def route(self, task_type: str, messages: list[dict],
                    temperature: float = 0.7, max_tokens: int | None = None,
                    stream: bool = False,
                    tools: list[dict] | None = None,
                    tool_choice: str | None = None,
                    timeout: int = 60,
                    user_openid: str = "",
                    session_id: str = "") -> str | object:
        config = ROUTE_TABLE.get(task_type, ROUTE_TABLE["chat"])
        model = config["model"]
        mt = max_tokens or config.get("max_tokens", 1500)

        self._cache_stats["total_calls"] += 1

        try:
            client = self._client
            if not client:
                raise RuntimeError("MiMo client not initialized, check MIMO_API_KEY")

            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": mt,
                "stream": stream,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice or "auto"

            response = await client.chat.completions.create(**kwargs)

            if stream:
                return response

            self._track_cache(response)
            await self._record_usage(task_type, model, response, user_openid, session_id)
            self._check_cache_health()

            if tools and response.choices[0].message.tool_calls:
                self._last_reasoning_content = getattr(response.choices[0].message, "reasoning_content", None) or None
                return response

            content = response.choices[0].message.content or ""
            rc = getattr(response.choices[0].message, "reasoning_content", None) or ""
            self._last_reasoning_content = rc if rc else None
            if not content:
                if rc:
                    content = rc
            return content

        except Exception as e:
            logger.error("router.call_failed", task=task_type, model=model,
                         error=f"{type(e).__name__}: {e}")
            raise

    def _track_cache(self, response):
        try:
            usage = response.usage
            if usage and hasattr(usage, "prompt_cache_hit_tokens"):
                self._cache_stats["hit_tokens"] += getattr(usage, "prompt_cache_hit_tokens", 0)
                self._cache_stats["miss_tokens"] += getattr(usage, "prompt_cache_miss_tokens", 0)
        except Exception:
            pass

    def _check_cache_health(self):
        now = time.time()
        if now - self._last_cache_warning < 300:
            return
        total = self._cache_stats["hit_tokens"] + self._cache_stats["miss_tokens"]
        if total > 10000:
            ratio = self._cache_stats["hit_tokens"] / total
            if ratio < 0.5:
                self._last_cache_warning = now
                logger.warning("router.cache_hit_low",
                               hit_ratio=f"{ratio:.1%}",
                               suggestion="考虑固定系统 prompt 前缀以提高缓存命中率")

    def get_cache_stats(self) -> dict:
        total = self._cache_stats["total_calls"]
        hit = self._cache_stats["hit_tokens"]
        miss = self._cache_stats["miss_tokens"]
        total_tokens = hit + miss
        return {
            "total_calls": total,
            "hit_tokens": hit,
            "miss_tokens": miss,
            "hit_ratio": round(hit / total_tokens, 3) if total_tokens > 0 else 0.0,
        }

    def pop_reasoning_content(self) -> str | None:
        rc = self._last_reasoning_content
        self._last_reasoning_content = None
        return rc
