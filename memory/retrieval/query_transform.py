"""查询理解/变换/多查询调度：简单判定、k 建议、rewrite+expand、多查询并行/串行。

拆分自 memory/_retrieval_engine.py（纯移动，行为零变化）。
方法经由 self._mm 访问 MemoryManager 依赖与状态，与拆分前语义完全一致。
"""
import asyncio
from typing import Any

from loguru import logger

from local_ai.integration.errors import is_structured_local_unavailable


class QueryTransformMixin:
    """查询变换与多查询调度组。"""

    def _is_retrieval_simple(self, query: str) -> bool:
        """A1: 判断查询是否足够简单，可跳过查询变换直接走混合检索

        P0 修复（用户要求"取消对话通道分类机制"）：
        移除对 SIMPLE_TASK_KEYWORDS 的依赖（已从 config.py 删除）。
        仅保留基于有效长度的启发式判断——这是检索层的查询变换优化，
        不影响对话主路径（所有消息仍统一走主路径，由 LLM 自行决定）。

        判定规则（按顺序短路）:
        1. 计算有效长度（中文字符 ×2 + 其他字符 ×1），<=15 直接判定为简单
        2. 有效长度 <=20 → 简单（中等长度，无需查询变换）
        3. 否则 → 非简单（长查询需要查询变换提升检索质量）
        """
        if not query:
            return True

        # 计算有效长度：中文字符 ×2 + 其他字符 ×1
        effective_len = 0
        for ch in query:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                effective_len += 2
            else:
                effective_len += 1

        # 规则 1：极短查询直接跳过变换
        if effective_len <= 15:
            return True

        # 规则 2：中等长度无需查询变换
        if effective_len <= 20:
            return True

        # 规则 3：长查询需要查询变换
        return False


    def _suggest_k(self, query: str, default_k: int = 8) -> int:
        """根据查询内容智能建议检索条数 k（情感陪伴型 bot）。

        策略：
        - 极短闲聊（问候/确认）：k=2，避免注入无关记忆
        - 日常闲聊：k=5~8
        - 情感/回忆/个人话题：k=10，多检索相关情感记忆
        - 涉及具体事件/人物/经历：k=10，召回更多上下文
        """
        if not query:
            return 1

        # 计算有效长度
        effective_len = 0
        for ch in query:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                effective_len += 2
            else:
                effective_len += 1

        # 情感/回忆/个人话题 → 多检索，让回复更有温度和连贯性
        # 注意：必须在长度检查之前，否则短查询会被提前截断
        emotional_indicators = (
            "记得", "想起", "回忆", "以前", "之前", "那时候", "那次",
            "喜欢", "讨厌", "开心", "难过", "伤心", "生气", "害怕",
            "担心", "焦虑", "压力", "累", "烦", "无聊", "孤独",
            "想你", "想ta", "分手", "吵架", "和好", "朋友", "家人",
            "爸妈", "生日", "节日", "考试", "面试", "工作", "辞职",
            "梦想", "未来", "以后", "遗憾", "后悔", "感恩", "幸福",
            "害怕", "勇敢", "加油", "坚持", "放弃", "努力",
            "心情", "感觉", "感受", "情绪", "状态", "最近",
        )
        query_lower = query.lower()
        for indicator in emotional_indicators:
            if indicator in query_lower:
                return min(10, default_k + 2)

        # 涉及具体事件/人物/经历
        event_indicators = (
            "发生", "那次", "那件事", "什么时候", "哪里", "谁",
            "聊天", "说过", "告诉你", "跟我说", "你记得",
            "上次", "上次说", "之前说", "你说过",
        )
        for indicator in event_indicators:
            if indicator in query_lower:
                return min(10, default_k + 1)

        # 极短查询：问候、确认、单字回复
        if effective_len <= 8:
            return 2

        # 短查询：简单闲聊
        if effective_len <= 15:
            return 5

        # 长查询：可能涉及多话题
        if effective_len > 60:
            return min(10, default_k + 2)

        return default_k


    async def _transform_queries(self, query: str, context: str) -> list[str]:
        """查询变换：rewrite + expand。A2 并行执行，失败降级到 [query]。

        MEMORY_RETRIEVAL_DIFFUSION=False 时跳过 expand_query（精准检索，搜什么就是什么），
        只保留 rewrite_query（查询改写优化表述，不是扩散）。
        """
        import config
        queries = [query]
        if not (self._mm._query_transformer and getattr(config, "QUERY_TRANSFORM_ENABLED", True)):
            return queries
        parallel_transform = getattr(config, "RETRIEVAL_PARALLEL_TRANSFORM", True)
        # 精准检索开关：False 时跳过 expand_query，只保留 rewrite_query
        _diffusion_enabled = getattr(config, "MEMORY_RETRIEVAL_DIFFUSION", False)
        try:
            if parallel_transform:
                queries = await self._transform_parallel(query, context, _diffusion_enabled)
            else:
                queries = await self._transform_serial(query, context, _diffusion_enabled)
        except Exception as e:
            logger.warning("memory.query_transform_failed", error=str(e))
        return queries


    async def _transform_parallel(self, query: str, context: str,
                                  diffusion_enabled: bool) -> list[str]:
        """并行/精准检索路径：rewrite + 可选 expand（各自独立 LLM 调用）。"""
        import config
        expand_count = getattr(config, "QUERY_EXPAND_COUNT", 2) if diffusion_enabled else 0
        rewrite_task = asyncio.create_task(
            self._mm._query_transformer.rewrite_query(query, context)
        )
        if expand_count > 0:
            expand_task = asyncio.create_task(
                self._mm._query_transformer.expand_query(query, n=expand_count)
            )
            rewritten, expanded = await asyncio.gather(
                rewrite_task, expand_task, return_exceptions=True
            )
            # 异常降级：rewrite 失败用原查询，expand 失败用 [query]
            if isinstance(rewritten, Exception):
                logger.warning("memory.rewrite_failed", error=str(rewritten))
                rewritten = query
            if isinstance(expanded, Exception):
                logger.warning("memory.expand_failed", error=str(expanded))
                expanded = [query]
            if not rewritten:
                rewritten = query
            if not expanded:
                expanded = [query]
            if rewritten != query:
                logger.debug("memory.query_rewritten",
                             original=query[:50], rewritten=rewritten[:50])
            # 合并：[rewritten] + [q for q in expanded if q != rewritten]
            merged = [rewritten]
            for q in expanded:
                if q != rewritten:
                    merged.append(q)
            queries = merged
            if len(queries) > 1:
                logger.debug("memory.query_expanded", count=len(queries))
            return queries

        # 精准检索：只执行 rewrite，不扩散
        rewritten = await rewrite_task
        if isinstance(rewritten, Exception):
            logger.warning("memory.rewrite_failed", error=str(rewritten))
            rewritten = query
        if not rewritten:
            rewritten = query
        if rewritten != query:
            logger.debug("memory.query_rewritten",
                         original=query[:50], rewritten=rewritten[:50])
        return [rewritten]


    async def _transform_serial(self, query: str, context: str,
                                diffusion_enabled: bool) -> list[str]:
        """串行降级路径：先 rewrite，再 expand。"""
        import config
        queries = [query]
        rewritten = await self._mm._query_transformer.rewrite_query(query, context)
        if rewritten and rewritten != query:
            queries = [rewritten]
            logger.debug("memory.query_rewritten", original=query[:50], rewritten=rewritten[:50])
        expand_count = getattr(config, "QUERY_EXPAND_COUNT", 2) if diffusion_enabled else 0
        if expand_count > 0:
            expanded = await self._mm._query_transformer.expand_query(rewritten, n=expand_count)
            if expanded and len(expanded) > 1:
                queries = expanded
                logger.debug("memory.query_expanded", count=len(queries))
        return queries


    async def _multi_query_parallel_search(self, queries: list[str], query: str,
                                             k: int,
                                             scope: Any | None = None) -> list[dict]:
        """A3: 并行多查询检索 + 批量 Reranker。

        各子查询检索时关闭内部 Reranker，统一在合并池上做一次批量精排。
        """
        precomputed_vecs = await self._batch_embed_queries(queries)
        all_results = await self._gather_hybrid_results(
            queries, precomputed_vecs, k, scope)
        return await self._batch_rerank(all_results, query, k)


    async def _batch_embed_queries(self, queries: list[str]) -> list[list[float] | None]:
        """P1-4: 合并 embed 批处理，子查询检索时复用向量，减少 embed 延迟与限流。

        批量失败时降级为 None，各子查询回退内部独立 embed（single-flight 兜底）。
        """
        precomputed_vecs: list[list[float] | None] = [None] * len(queries)
        if getattr(self._mm, "vec", None):
            try:
                batch_vecs = await self._mm.vec.embed(list(queries))
                for i, v in enumerate(batch_vecs):
                    if v:
                        precomputed_vecs[i] = v
            except Exception as e:
                logger.debug("memory.batch_embed_failed", error=str(e))
        return precomputed_vecs


    async def _gather_hybrid_results(self, queries: list[str],
                                     precomputed_vecs: list[list[float] | None],
                                     k: int, scope: Any | None) -> list[dict]:
        """并行执行各子查询的 hybrid 检索，去重合并候选池。"""
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        hybrid_tasks = [
            self._mm.retrieve_memories_hybrid(
                q, k=k * 2, use_reranker=False, scope=scope,
                query_vec=precomputed_vecs[i],
            )
            for i, q in enumerate(queries)
        ]
        hybrid_results = await asyncio.gather(*hybrid_tasks, return_exceptions=True)
        for i, res in enumerate(hybrid_results):
            if isinstance(res, Exception):
                if is_structured_local_unavailable(res):
                    raise res
                logger.warning("memory.hybrid_search_failed",
                               query=queries[i][:50], error=str(res))
                continue
            for r in res:
                rid = str(r.get("id", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    all_results.append(r)
        return all_results


    async def _batch_rerank(self, all_results: list[dict], query: str,
                            k: int) -> list[dict]:
        """批量 Reranker：对合并后的候选池用原始 query 重排一次。"""
        if self._mm._reranker and self._mm._reranker.available and len(all_results) > k:
            try:
                docs = [r.get("summary", "") for r in all_results]
                # P1-5: 移除 5s 外层 wait_for（治标）。
                # 根因：reranker 已用共享 httpx client（connect=15s）+ 单次请求 5s timeout，
                # 且 _hybrid_rerank 与本方法均有 try/except 降级。原外层 5s 与内层 5s
                # 双层超时，外层必然先触发，reranker 实际耗时被截断，降级机制失效。
                reranked = await self._mm._reranker.rerank(
                    query=query,
                    documents=docs,
                    top_n=k,
                )
                reranked_results = []
                for item in reranked:
                    idx = item.get("index", -1)
                    if 0 <= idx < len(all_results):
                        mem = all_results[idx]
                        mem["rerank_score"] = item.get("relevance_score", 0.0)
                        mem["score_kind"] = "rerank"
                        reranked_results.append(mem)
                if reranked_results:
                    all_results = reranked_results
            except Exception as e:
                if is_structured_local_unavailable(e):
                    raise
                logger.warning("memory.batch_rerank_failed", error=str(e))
        return all_results


    async def _multi_query_serial_search(self, queries: list[str], k: int,
                                           scope: Any | None = None) -> list[dict]:
        """串行降级（原有逻辑）。"""
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        for q in queries:
            try:
                hybrid_results = await self._mm.retrieve_memories_hybrid(q, k=k, scope=scope)
                for r in hybrid_results:
                    rid = str(r.get("id", ""))
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        all_results.append(r)
            except Exception as e:
                if is_structured_local_unavailable(e):
                    raise
                logger.warning("memory.hybrid_search_failed", query=q[:50], error=str(e))
        return all_results
