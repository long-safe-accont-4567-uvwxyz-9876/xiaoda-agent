# query_transform.py — 查询改写与扩展（使用硅基流动免费模型，不占用主模型配额）
from typing import Any, ClassVar
import os
import asyncio
import httpx
from loguru import logger


class QueryTransformer:
    """查询变换器：改写/扩展/分解用户原始查询

    使用硅基流动免费模型（如 Qwen3-8B），不占用主模型调用配额。
    无 API Key 时自动降级返回原始查询。
    """

    # 硅基流动免费模型列表（0 成本）
    FREE_MODELS: ClassVar[list[str]] = [
        "Qwen/Qwen2.5-7B-Instruct",
        "THUDM/glm-4-9b-chat",
        "internlm/internlm3-8b-instruct",
    ]

    # 意图分类关键词（规则匹配快速路径）
    TEMPORAL_KEYWORDS: ClassVar[set[str]] = {"昨天", "前天", "今天", "上周", "上个月", "刚才", "之前", "那次", "那天", "那次对话"}
    CHAT_KEYWORDS: ClassVar[set[str]] = {"你好", "嗨", "谢谢", "再见", "哈哈", "早安", "晚安", "在吗", "在不在"}
    MULTIHOP_KEYWORDS: ClassVar[set[str]] = {"和", "与", "比较", "区别", "关系", "之间", "哪个好", "对比"}

    def __init__(self, router: Any | None=None, api_key: str = "", base_url: str = "",
                 model: str = "") -> None:
        self._router = router  # 保留兼容，但不再用于查询变换
        self._api_key = api_key or os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._base_url = base_url or "https://api.siliconflow.cn/v1"
        self._model = model or os.getenv("QUERY_TRANSFORM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
        self._available = bool(self._api_key)

    @property
    def available(self) -> bool:
        return self._available

    async def _call_free_model(self, prompt: str, temperature: float = 0.1,
                                max_tokens: int = 150) -> str | None:
        """调用硅基流动免费模型"""
        if not self._available:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
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
                )
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices", [])
                if not choices:
                    return None
                content = choices[0].get("message", {}).get("content", "")
                return content.strip() if content else None
        except Exception as e:
            logger.warning("query_transform.free_model_failed", model=self._model, error=str(e))
            return None

    async def rewrite_query(self, original_query: str, context: str = "") -> str:
        """将口语化查询改写为更适合检索的形式"""
        if not self._available:
            return original_query

        prompt = f"""将以下用户查询改写为更适合文档检索的关键词查询。
保持语义不变，去除口语化表达，补充必要的上下文信息。
只输出改写后的查询，不要解释。

原始查询: {original_query}
对话上下文: {context[-200:] if context else '无'}

改写后的查询:"""

        result = await self._call_free_model(prompt, temperature=0.1, max_tokens=100)
        return result if result else original_query

    async def expand_query(self, query: str, n: int = 3) -> list[str]:
        """生成 n 个不同视角的查询扩展"""
        if not self._available:
            return [query]

        prompt = f"""为以下查询生成 {n} 个不同视角的搜索查询，用于提高检索召回率。
每行一个查询，不要编号，不要解释。

原始查询: {query}"""

        result = await self._call_free_model(prompt, temperature=0.3, max_tokens=150)
        if result:
            expanded = [line.strip() for line in result.strip().split("\n") if line.strip()]
            return [query, *expanded[:n]]
        return [query]

    async def generate_hyde_document(self, query: str, context: str = "") -> str | None:
        """生成假设答案文档用于 HyDE 向量混合

        LLM 生成一个简短的假设性答案，用于其嵌入向量增强检索。
        超时 5s 降级返回 None。
        """
        if not self._available:
            return None

        prompt = f"""请根据以下问题，写一段简短的假设性答案（50-100字）。
不需要完全正确，只需要语义上与答案文档接近。
只输出答案文本，不要解释。

问题: {query}
"""
        try:
            return await asyncio.wait_for(
                self._call_free_model(prompt, temperature=0.3, max_tokens=100),
                timeout=5.0,
            )
        except TimeoutError:
            logger.warning("query_transform.hyde_timeout", query=query[:50])
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

        for kw in self.CHAT_KEYWORDS:
            if kw in query:
                return "chat"

        for kw in self.TEMPORAL_KEYWORDS:
            if kw in query:
                return "temporal"

        for kw in self.MULTIHOP_KEYWORDS:
            if kw in query:
                return "multi-hop"

        # LLM 可用时，对非明确类型走 LLM 分类
        if self._available:
            prompt = f"请分类以下查询的意图类型（temporal/factual/chat/multi-hop），只输出类型名称：\n查询: {query}"
            try:
                result = await asyncio.wait_for(
                    self._call_free_model(prompt, temperature=0.0, max_tokens=20),
                    timeout=5.0,
                )
                if result:
                    result_clean = result.strip().lower()
                    for intent in ("temporal", "multi-hop", "factual", "chat"):
                        if intent in result_clean:
                            return intent
            except Exception as e:
                logger.warning("query_transform.classify_intent_failed", error=str(e))

        # 默认 factual
        return "factual"