# query_transform.py — 查询改写与扩展（使用硅基流动免费模型，不占用主模型配额）
import os
import httpx
from loguru import logger


class QueryTransformer:
    """查询变换器：改写/扩展/分解用户原始查询

    使用硅基流动免费模型（如 Qwen3-8B），不占用主模型调用配额。
    无 API Key 时自动降级返回原始查询。
    """

    # 硅基流动免费模型列表（0 成本）
    FREE_MODELS = [
        "Qwen/Qwen3-8B",
        "THUDM/glm-4-9b-chat",
        "internlm/internlm3-8b-instruct",
    ]

    def __init__(self, router=None, api_key: str = "", base_url: str = "",
                 model: str = ""):
        self._router = router  # 保留兼容，但不再用于查询变换
        self._api_key = api_key or os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._base_url = base_url or "https://api.siliconflow.cn/v1"
        self._model = model or os.getenv("QUERY_TRANSFORM_MODEL", "Qwen/Qwen3-8B")
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
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
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
            return [query] + expanded[:n]
        return [query]
