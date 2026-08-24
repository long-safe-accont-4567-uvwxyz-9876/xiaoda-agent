# query_transform.py — 查询改写与扩展（使用硅基流动免费模型，不占用主模型配额）
from typing import Any, ClassVar
import os
import asyncio
import hashlib
import time
from collections import OrderedDict
import httpx
from loguru import logger

from utils.http_pool import get_shared_client
from utils.free_model_backend import call_local_model


# G15: sentinel 用于区分"缓存未命中"和"命中 None"
# 不能用 None 作为 sentinel，因为 None 是合法的缓存值（LLM 可能返回 None）
_CACHE_MISS = object()


REWRITE_PROMPT = """将以下用户查询改写为更适合文档检索的关键词查询。
要求：
1. 保留核心语义，去除口语化表达
2. 推断可能的关联关键词并补充（如"饮食偏好"补充"香菜 豆浆 川菜 咖啡"，"后端代码"补充"Python FastAPI SQLAlchemy Docker"）
3. 只输出改写后的查询，不要解释

原始查询: {original_query}
对话上下文: {context_block}

改写后的查询:"""

EXPAND_PROMPT = """为以下查询生成 {n} 个不同视角的搜索查询，用于提高检索召回率。
每行一个查询，不要编号，不要解释。

原始查询: {query}"""

HYDE_PROMPT = """请根据以下问题，写一段简短的假设性答案（50-100字）。
不需要完全正确，只需要语义上与答案文档接近。
只输出答案文本，不要解释。

问题: {query}
"""

CLASSIFY_PROMPT = "请分类以下查询的意图类型（temporal/factual/chat/multi-hop），只输出类型名称：\n查询: {query}"


class QueryTransformer:
    """查询变换器：改写/扩展/分解用户原始查询

    使用硅基流动免费模型，不占用主模型调用配额。
    无 API Key 时自动降级返回原始查询。
    """

    # 硅基流动免费模型列表（0 成本）
    # 注意：避免使用 GLM-Z1 等思考模型，因为 max_tokens 较小时会返回思考碎片污染查询
    FREE_MODELS: ClassVar[list[str]] = [
        "THUDM/GLM-4-9B-0414",
        "Qwen/Qwen2.5-7B-Instruct",
        "THUDM/glm-4-9b-chat",
    ]

    # 意图分类关键词（规则匹配快速路径）
    TEMPORAL_KEYWORDS: ClassVar[set[str]] = {"昨天", "前天", "今天", "上周", "上个月", "刚才", "之前", "那次", "那天", "那次对话", "刚刚", "小时前", "分钟前"}
    CHAT_KEYWORDS: ClassVar[set[str]] = {
        # 问候类
        "你好", "嗨", "谢谢", "再见", "哈哈", "早安", "晚安", "在吗", "在不在",
        # 情绪/感受类（闲聊高频）
        "觉得", "感觉", "开心", "难过", "无聊", "累了", "好困", "好饿",
        "喜欢", "讨厌", "害怕", "担心", "兴奋", "感动",
        # 日常闲聊类
        "在干嘛", "干嘛呢", "吃了吗", "早上好", "晚上好", "中午好",
        "你知道吗", "告诉你", "跟你说", "我说", "好吧", "算了",
        "好的", "嗯嗯", "嘿嘿", "嘻嘻", "呵呵",
    }
    MULTIHOP_KEYWORDS: ClassVar[set[str]] = {"和", "与", "比较", "区别", "关系", "之间", "哪个好", "对比"}

    # G15: LRU + TTL 缓存配置
    TRANSFORM_CACHE_MAXSIZE = 100
    TRANSFORM_CACHE_TTL = 600  # 10 分钟（秒）

    def __init__(self, router: Any | None=None, api_key: str = "", base_url: str = "",
                 model: str = "", backend: str = "api") -> None:
        # backend: local=走本地模型；api=硅基流动免费模型；off=禁用（无 auto）
        self._router = router
        self._api_key = api_key or os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._base_url = base_url or "https://api.siliconflow.cn/v1"
        self._model = model or os.getenv("QUERY_TRANSFORM_MODEL", "THUDM/GLM-4-9B-0414")
        backend = "api" if backend == "auto" else backend
        self._backend = backend if backend in ("local", "api", "off") else "api"
        self._local_model = None  # 功能节点独立选择的本地模型（None=全局共享）
        if self._backend == "off":
            self._available = False
        elif self._backend == "local":
            self._available = bool(router)
        else:
            self._available = bool(self._api_key)

        # G15: LRU + TTL 缓存，避免重复调 LLM
        # key -> (result, expire_at_monotonic)
        self._rewrite_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._expand_cache: OrderedDict[str, tuple[list[str], float]] = OrderedDict()
        self._hyde_cache: OrderedDict[str, tuple[str | None, float]] = OrderedDict()

    def set_backend(self, backend: str, local_model: str | None = None) -> None:
        """热更新后端选择（local/api/off；历史值 auto 按 api）。"""
        backend = "api" if backend == "auto" else backend
        if backend not in ("local", "api", "off"):
            return
        self._backend = backend
        if local_model is not None:
            self._local_model = local_model
        if backend == "off":
            self._available = False
        elif backend == "local":
            self._available = bool(self._router)
        else:
            self._available = bool(self._api_key)
        logger.info("query_transform.backend_set backend={} available={}", backend, self._available)

    def _cache_get(self, cache: OrderedDict, key: str) -> Any:
        """G15: 从缓存取值，TTL 过期或未命中返回 _CACHE_MISS sentinel。

        使用 sentinel 而非 None，以便调用方区分"未命中"和"命中 None"——
        LLM 可能合法地返回 None（如思考碎片被过滤），此时也需要缓存。
        """
        if key not in cache:
            return _CACHE_MISS
        result, expire_at = cache[key]
        if time.monotonic() > expire_at:
            cache.pop(key, None)
            return _CACHE_MISS
        cache.move_to_end(key)
        return result

    # 降级结果的短 TTL：LLM 超时/失败时缓存原查询，防止连续超时尖峰，
    # 但不能按 10 分钟长 TTL 缓存——一次网络抖动会让后续 10 分钟都拿不到
    # 改写结果，持续降低检索精度。60s 兼顾"防尖峰"与"瞬时故障可恢复"。
    TRANSFORM_FALLBACK_TTL = 60

    def _cache_put(self, cache: OrderedDict, key: str, value: Any,
                   ttl: float | None = None) -> None:
        """G15: 写入缓存，维护 maxsize。ttl 为 None 时用默认 TTL。"""
        cache[key] = (value, time.monotonic() + (
            self.TRANSFORM_CACHE_TTL if ttl is None else ttl))
        cache.move_to_end(key)
        if len(cache) > self.TRANSFORM_CACHE_MAXSIZE:
            cache.popitem(last=False)

    def clear_cache(self) -> None:
        """G15: 清空所有查询变换缓存。"""
        self._rewrite_cache.clear()
        self._expand_cache.clear()
        self._hyde_cache.clear()

    @property
    def available(self) -> bool:
        return self._available

    async def _call_free_model(self, prompt: str, temperature: float = 0.1,
                                max_tokens: int = 150) -> str | None:
        """执行查询变换：backend=local 时用本地模型，否则走硅基流动免费模型。"""
        if not self._available:
            return None
        if self._backend == "local":
            # 本地模型执行（功能节点独立选模型）
            return await call_local_model(
                self._router,
                [{"role": "user", "content": prompt}],
                temperature,
                max_tokens,
                model_id=self._local_model,
            )
        try:
            # G4: 共享 httpx.AsyncClient（连接池复用 + HTTP/2），单次请求级别覆盖 timeout
            # 修复：15s→4s。根因：检索整体只有 8s 超时，free model 15s 超时远超预算，
            # 硅基流动慢时 rewrite/expand 卡住 → 8s 超时杀掉整个检索 → 零记忆
            # 4s：足够免费模型响应（通常 1-2s），慢时快速失败用原始查询兜底
            client = get_shared_client()
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(4.0),
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            content = choices[0].get("message", {}).get("content", "")
            if not content:
                return None
            content = content.strip()
            # 防御思考碎片泄漏：GLM-Z1 等思考模型可能返回思考过程碎片
            # 这些碎片不是有效的查询改写，会导致记忆检索错误→答非所问
            _THINKING_PREFIXES = (
                "首先", "让我", "分析", "我需要", "根据", "好的", "嗯",
                "用户", "任务是", "思考", "我来", "接下来",
            )
            if any(content.startswith(p) for p in _THINKING_PREFIXES):
                logger.warning("query_transform.thinking_fragment_detected",
                               model=self._model, content_preview=content[:60])
                return None
            return content
        except Exception as e:
            # 修复 P2 Bug 8: 已有降级到 router 兜底，降级为 debug
            logger.debug("query_transform.free_model_failed", model=self._model, error=str(e)[:200], error_type=type(e).__name__)
            return None

    async def rewrite_query(self, original_query: str, context: str = "") -> str:
        """将口语化查询改写为更适合检索的形式

        G15: 按 query+context_hash 缓存，TTL 10 分钟。
        """
        if not self._available:
            return original_query

        # G15: 缓存命中检查
        cache_key = hashlib.md5(f"{original_query}|{context[-200:]}".encode("utf-8"), usedforsecurity=False).hexdigest()
        cached = self._cache_get(self._rewrite_cache, cache_key)
        if cached is not _CACHE_MISS:
            return cached

        context_block = context[-200:] if context else '无'
        override = None
        try:
            from web.prompt_profile_repository import try_resolve

            override = try_resolve("query.rewrite", {
                "original_query": original_query, "context_block": context_block,
            })
        except Exception:
            override = None
        if override is not None:
            prompt = override[1]
        else:
            # 防御性加固：查询/上下文可能含 {} 字符，用 str.replace 替代 f-string
            prompt = (
                REWRITE_PROMPT
                .replace("{original_query}", original_query)
                .replace("{context_block}", context_block)
            )

        # CodeRabbit #2: 加 asyncio 层超时防线（httpx 4s 之外的兜底，防止
        # 客户端重试/连接建立慢导致 retrieve_memories 整体超时）。与 hyde/classify 一致。
        # CodeRabbit #D: 超时降级为原查询后继续走缓存流程（与 result=None 的正常
        # 降级一致），避免网络持续慢时每次检索都重复 5s 超时拖慢流水线。
        #
        # I3 注释（代码审查一致性）：此处的"降级"与 model_router.py 删除 ImportError
        # fallback 的"fail-fast"理念相反，但场景不同，不冲突：
        # - model_router 的 fallback 是"损坏状态降级"——盲拼接 prefill 内容会引入重复
        #   和人格劫持（事故 ID 2112），故选择 fail-fast 让 ImportError 直接抛出。
        # - 此处的降级是"功能降级"——原查询本身仍可用作检索输入，仅失去 LLM 改写
        #   带来的精度提升，不产生损坏状态。缓存原查询还能避免重复超时尖峰。
        # 两者判断依据是"降级后状态是否损坏"，而非"是否一致"。后续维护者请勿为
        # 统一理念而删除此处降级。
        try:
            result = await asyncio.wait_for(
                self._call_free_model(prompt, temperature=0.1, max_tokens=100),
                timeout=5.0,
            )
        except TimeoutError:
            logger.warning("query_transform.rewrite_timeout", query=original_query[:50])
            result = None  # 走降级路径，final=original_query 并缓存
        final = result if result else original_query
        # G15: 写入缓存（即使降级为原查询也缓存，避免重复调 LLM）
        # 降级结果用短 TTL：瞬时抖动不应把未改写的查询钉住 10 分钟
        self._cache_put(self._rewrite_cache, cache_key, final,
                        ttl=None if result else self.TRANSFORM_FALLBACK_TTL)
        return final

    async def expand_query(self, query: str, n: int = 3) -> list[str]:
        """生成 n 个不同视角的查询扩展

        G15: 按 query+n 缓存，TTL 10 分钟。
        """
        if not self._available:
            return [query]

        # G15: 缓存命中检查
        cache_key = hashlib.md5(f"{query}|{n}".encode("utf-8"), usedforsecurity=False).hexdigest()
        cached = self._cache_get(self._expand_cache, cache_key)
        if cached is not _CACHE_MISS:
            return list(cached)  # 返回副本，避免外部修改污染缓存

        override = None
        try:
            from web.prompt_profile_repository import try_resolve

            override = try_resolve("query.expand", {
                "n": str(n), "query": query,
            })
        except Exception:
            override = None
        if override is not None:
            prompt = override[1]
        else:
            prompt = (
                EXPAND_PROMPT
                .replace("{n}", str(n))
                .replace("{query}", query)
            )

        # CodeRabbit #2: 加 asyncio 层超时防线，超时降级返回 [query]
        # CodeRabbit #D: 超时降级后继续走缓存流程（与 result=None 一致）
        try:
            result = await asyncio.wait_for(
                self._call_free_model(prompt, temperature=0.3, max_tokens=150),
                timeout=5.0,
            )
        except TimeoutError:
            logger.warning("query_transform.expand_timeout", query=query[:50])
            result = None  # 走降级路径，final=[query] 并缓存
        if result:
            # 保序去重 + 过滤原查询：模型常重复输出同一行或复述原问题，
            # 原实现 [query, *expanded[:n]] 会让重复项挤占检索名额，
            # 每项都会走一次向量/FTS/Rerank，白耗预算且降低召回多样性
            seen = {query}
            unique_expanded: list[str] = []
            for line in result.strip().split("\n"):
                line = line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                unique_expanded.append(line)
                if len(unique_expanded) >= n:
                    break
            final = [query, *unique_expanded]
        else:
            final = [query]
        # G15: 写入缓存（降级结果用短 TTL，同 rewrite_query）
        self._cache_put(self._expand_cache, cache_key, final,
                        ttl=None if result else self.TRANSFORM_FALLBACK_TTL)
        return list(final)  # 返回副本

    async def generate_hyde_document(self, query: str, context: str = "") -> str | None:
        """生成假设答案文档用于 HyDE 向量混合

        G15: 按 query+context_hash 缓存，TTL 10 分钟。
        超时 5s 降级返回 None。
        """
        if not self._available:
            return None

        # G15: 缓存命中检查
        cache_key = hashlib.md5(f"{query}|{context[-200:]}".encode("utf-8"), usedforsecurity=False).hexdigest()
        cached = self._cache_get(self._hyde_cache, cache_key)
        if cached is not _CACHE_MISS:
            return cached

        override = None
        try:
            from web.prompt_profile_repository import try_resolve

            override = try_resolve("query.hyde", {"query": query})
        except Exception:
            override = None
        if override is not None:
            prompt = override[1]
        else:
            prompt = HYDE_PROMPT.replace("{query}", query)
        try:
            result = await asyncio.wait_for(
                self._call_free_model(prompt, temperature=0.3, max_tokens=100),
                timeout=5.0,
            )
            # G15: 写入缓存（包括 None 结果，避免重复调 LLM）
            self._cache_put(self._hyde_cache, cache_key, result)
            return result
        except TimeoutError:
            logger.warning("query_transform.hyde_timeout", query=query[:50])
            # G15: timeout 不缓存（瞬时网络问题，下次重试可能成功）
            return None
        except Exception as e:
            logger.warning("query_transform.hyde_failed", error=str(e))
            return None

    async def classify_intent(self, query: str) -> str:
        """分类查询意图

        返回: temporal / factual / chat / multi-hop
        LLM 不可用时降级走规则匹配。
        """
        # 规则匹配（快速路径）
        if len(query) < 5:
            return "chat"

        # 时间判定优先于闲聊判定：
        # 原顺序先匹配 CHAT_KEYWORDS，导致"我昨天觉得很难过"被归为 chat，
        # 错过时间优先的记忆检索。含明确时间表达式时时间信号更强，应先返回
        # temporal，让检索走时间范围路径。
        for kw in self.TEMPORAL_KEYWORDS:
            if kw in query:
                return "temporal"

        # 绝对日期模式匹配：N月N号/N月N日/今天早上/昨天晚上 等
        # TEMPORAL_KEYWORDS 只含相对时间词，需补充绝对日期模式
        # 否则"7月15号早上7点到8点"会被误分类为 factual，导致 k 被限制为 3
        import re as _re
        if _re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*[号日]", query):
            return "temporal"
        if _re.search(r"\d{1,2}\s*[点时:：]\s*(\d{1,2})?\s*分?\s*(到|~|-|—)", query):
            return "temporal"

        for kw in self.CHAT_KEYWORDS:
            if kw in query:
                return "chat"

        for kw in self.MULTIHOP_KEYWORDS:
            if kw in query:
                return "multi-hop"

        # LLM 可用时，对非明确类型走 LLM 分类
        # 性能优化：LLM 调用会增加 200-800ms 延迟，默认关闭，规则未命中直接返回 factual
        # 如需更精确的分类，设置 INTENT_LLM_CLASSIFY=true 启用
        try:
            import config as _cfg
            llm_classify = getattr(_cfg, "INTENT_LLM_CLASSIFY", False)
        except (ImportError, AttributeError):
            llm_classify = False

        if llm_classify and self._available:
            override = None
            try:
                from web.prompt_profile_repository import try_resolve

                override = try_resolve("query.classify", {"query": query})
            except Exception:
                override = None
            if override is not None:
                prompt = override[1]
            else:
                prompt = CLASSIFY_PROMPT.replace("{query}", query)
            try:
                _cfg_timeout = getattr(_cfg, "INTENT_CLASSIFY_TIMEOUT", 5.0)
                result = await asyncio.wait_for(
                    self._call_free_model(prompt, temperature=0.0, max_tokens=20),
                    timeout=_cfg_timeout,
                )
                if result:
                    result_clean = result.strip().lower()
                    for intent in ("temporal", "multi-hop", "factual", "chat"):
                        if intent in result_clean:
                            return intent
            except Exception as e:
                logger.warning("query_transform.classify_intent_failed", error=str(e), error_type=type(e).__name__)

        # 默认 factual
        return "factual"