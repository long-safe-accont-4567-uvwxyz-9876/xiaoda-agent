# query_transform.py — 查询改写与扩展

class QueryTransformer:
    """查询变换器：改写/扩展/分解用户原始查询"""

    def __init__(self, router=None):
        self._router = router

    async def rewrite_query(self, original_query: str, context: str = "") -> str:
        """将口语化查询改写为更适合检索的形式"""
        if not self._router:
            return original_query

        prompt = f"""将以下用户查询改写为更适合文档检索的关键词查询。
保持语义不变，去除口语化表达，补充必要的上下文信息。
只输出改写后的查询，不要解释。

原始查询: {original_query}
对话上下文: {context[-200:] if context else '无'}

改写后的查询:"""

        result = await self._router.route(
            "chat_flash",
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100,
        )
        return result.strip() if result else original_query

    async def expand_query(self, query: str, n: int = 3) -> list[str]:
        """生成 n 个不同视角的查询扩展"""
        if not self._router:
            return [query]

        prompt = f"""为以下查询生成 {n} 个不同视角的搜索查询，用于提高检索召回率。
每行一个查询，不要编号，不要解释。

原始查询: {query}"""

        result = await self._router.route(
            "chat_flash",
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=150,
        )
        if result:
            expanded = [line.strip() for line in result.strip().split("\n") if line.strip()]
            return [query] + expanded[:n]
        return [query]
