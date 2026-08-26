"""检索通道实现：FTS/Vec/HyDE/扩散/时间/对话日志 + ContextNest 确定性 selector。

拆分自 memory/_retrieval_engine.py（纯移动，行为零变化）。
方法经由 self._mm 访问 MemoryManager 依赖与状态，与拆分前语义完全一致。
RecallChannels（七路召回结果打包）定义于此：fusion 的签名引用它，
放在 channels 可避免 fusion → pipeline 循环依赖；pipeline 再 re-export。
"""
import inspect
import time
from typing import Any, NamedTuple

from loguru import logger

from memory._memory_utils import (
    _natural_time_desc,
    _parse_temporal_query,
    _stage_log,
)


class RecallChannels(NamedTuple):
    """七路召回结果打包（温/热用户并行召回的一次性产物）。

    原先 `_run_multi_recall` 返回裸 7 元组，`_resolve_fallback_or_single_channel`
    与 `_fuse_and_rank` 各自接收 7 个通道位置参数（总参数 16 个）。
    打包为 NamedTuple 后签名降至 8/9 参数，字段名自文档化。
    """
    fts_items: list
    vec_items: list
    kg_items: list
    child_items: list
    spread_items: list
    entity_items: list
    kg_v2_items: list


def _normalize_vector_hits(vec_results: Any) -> tuple[list[tuple[int, float]], dict[int, dict]]:
    """归一化向量检索命中为 (rowid, distance) 列表 + 附带完整记录表。

    兼容 dict 命中（含 rowid/id/distance 与 summary 等字段）与二元组命中。
    """
    normalized: list[tuple[int, float]] = []
    originals: dict[int, dict] = {}
    for hit in vec_results or []:
        if isinstance(hit, dict):
            row_id = hit.get("rowid", hit.get("id"))
            distance = hit.get("distance")
        else:
            try:
                row_id, distance = hit
            except (TypeError, ValueError):
                continue
        if row_id is None or distance is None:
            continue
        try:
            normalized_row_id = int(row_id)
            normalized.append((normalized_row_id, float(distance)))
            if isinstance(hit, dict) and any(
                key in hit
                for key in (
                    "summary", "content", "status", "is_raw", "user_id"
                )
            ):
                original = dict(hit)
                original.setdefault("id", normalized_row_id)
                originals[normalized_row_id] = original
        except (TypeError, ValueError):
            continue
    return normalized, originals


def _merge_vector_page_hits(
    collected: list[tuple[int, float]],
    page_hits: list[tuple[int, float]],
    seen_ids: set[int],
    limit: int,
) -> None:
    """跨页合并向量命中：按 (distance, row_id) 升序维护全局 top-N。

    同一 rowid 只保留首次（最近页）命中；截断保证候选规模不随页数膨胀。
    """
    fresh = [hit for hit in page_hits if hit[0] not in seen_ids]
    seen_ids.update(row_id for row_id, _ in fresh)
    merged = sorted((*collected, *fresh), key=lambda item: (item[1], item[0]))
    collected[:] = merged[:limit]


class RecallChannelMixin:
    """检索通道实现组：向量/HyDE/扩散/时间/对话日志/确定性 selector。"""

    @staticmethod
    def _explicit_attr(owner: Any, name: str) -> Any | None:
        """Read only real attributes, ignoring MagicMock's dynamic children."""
        if owner is None:
            return None
        try:
            inspect.getattr_static(owner, name)
        except AttributeError:
            return None
        return getattr(owner, name, None)

    def _memory_repositories(self) -> list[Any]:
        repositories: list[Any] = []
        direct = self._explicit_attr(self._mm, "memory")
        if direct is not None:
            repositories.append(direct)
        db = self._explicit_attr(self._mm, "db")
        nested = self._explicit_attr(db, "memory")
        if nested is not None and all(nested is not repo for repo in repositories):
            repositories.append(nested)
        return repositories

    @staticmethod
    def _mark_scope_scan_partial(component: str, pages: int, candidates: int) -> None:
        from memory.retrieval.trace import mark_retrieval_degraded
        from utils.metrics import metrics

        mark_retrieval_degraded("scope_scan_partial")
        metrics.inc("retrieval.scope_scan.partial")
        logger.warning(
            "memory.scope_scan_partial",
            component=component,
            pages=pages,
            candidates=candidates,
        )

    async def _iter_visible_candidate_pages(
        self,
        scope: Any | None,
        candidate_ids: list[int] | None = None,
        *,
        children: bool = False,
        component: str = "vector",
    ):
        """Yield recent-first scoped ID pages until exhausted or budget-limited."""
        if scope is None:
            yield candidate_ids
            return

        page_method_name = (
            "get_visible_child_id_page" if children
            else "get_visible_memory_id_page"
        )
        repository = next(
            (
                repo for repo in self._memory_repositories()
                if inspect.iscoroutinefunction(
                    self._explicit_attr(repo, page_method_name)
                )
            ),
            None,
        )
        if repository is None:
            legacy = await self._get_visible_candidate_ids(
                scope, candidate_ids, children=children
            )
            yield legacy
            return

        import config

        page_size = max(1, int(getattr(config, "SCOPE_SCAN_PAGE_SIZE", 1000)))
        max_pages = max(1, int(getattr(config, "SCOPE_SCAN_MAX_PAGES", 100)))
        max_candidates = max(
            page_size,
            int(getattr(config, "SCOPE_SCAN_MAX_CANDIDATES", 100000)),
        )
        budget_ms = max(1, int(getattr(config, "SCOPE_SCAN_BUDGET_MS", 5000)))
        selected = set(candidate_ids) if candidate_ids is not None else None
        cursor: int | None = None
        pages = 0
        scanned = 0
        started = time.monotonic()
        has_more = False
        page_method = getattr(repository, page_method_name)

        while pages < max_pages and scanned < max_candidates:
            if pages and (time.monotonic() - started) * 1000 >= budget_ms:
                break
            current_size = min(page_size, max_candidates - scanned)
            page = await page_method(
                scope, before_id=cursor, page_size=current_size
            )
            raw_ids = [int(value) for value in page.get("ids", [])]
            pages += 1
            scanned += len(raw_ids)
            has_more = bool(page.get("has_more"))
            page_ids = (
                raw_ids if selected is None
                else [value for value in raw_ids if value in selected]
            )
            if page_ids:
                yield page_ids
            if selected is not None:
                selected.difference_update(page_ids)
                if not selected:
                    has_more = False
                    break
            if not has_more or not raw_ids:
                break
            next_cursor = page.get("next_cursor")
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = int(next_cursor)

        if has_more:
            self._mark_scope_scan_partial(component, pages, scanned)

    async def _get_visible_candidate_ids(
        self,
        scope: Any | None,
        candidate_ids: list[int] | None = None,
        *,
        children: bool = False,
        limit: int = 50000,
    ) -> list[int] | None:
        """Resolve a bounded DB-filtered candidate set before vector top-k."""
        if scope is None:
            return candidate_ids
        method_name = "get_visible_child_ids" if children else "get_visible_memory_ids"
        capable = False
        for repository in self._memory_repositories():
            method = self._explicit_attr(repository, method_name)
            if method is None or not inspect.iscoroutinefunction(method):
                continue
            capable = True
            try:
                visible = method(scope, limit=limit)
                if not inspect.isawaitable(visible):
                    continue
                visible_ids = await visible
                if not isinstance(visible_ids, list):
                    continue
                if candidate_ids is None:
                    return [int(value) for value in visible_ids]
                selected = set(candidate_ids)
                return [int(value) for value in visible_ids if value in selected]
            except Exception as exc:
                logger.debug(
                    "memory.vector_candidate_scope_failed",
                    children=children,
                    error=str(exc),
                )
        return [] if capable else candidate_ids

    async def _get_visible_vector_memories(
        self,
        ids: list[int],
        scope: Any | None,
        original_memories: dict[int, dict],
    ) -> list[dict]:
        """Backfill vector hits through active visibility when the DB supports it."""
        repositories = self._memory_repositories()
        visibility_capable: set[int] = set()
        visibility_failed: set[int] = set()

        for repository in repositories:
            method = self._explicit_attr(repository, "get_visible_memories_by_ids")
            if method is None or not inspect.iscoroutinefunction(method):
                continue
            visibility_capable.add(id(repository))
            try:
                result = method(ids, scope=scope)
                if inspect.isawaitable(result):
                    rows = await result
                    if isinstance(rows, list):
                        return rows
            except Exception as exc:
                visibility_failed.add(id(repository))
                logger.debug("memory.vector_visibility_filter_failed", error=str(exc))

        # Legacy test doubles expose only get_memories_by_ids. If a real repository's
        # visibility query fails, preserve the channel but enforce active status locally.
        for repository in repositories:
            if (
                id(repository) in visibility_capable
                and id(repository) not in visibility_failed
            ):
                continue
            method = self._explicit_attr(repository, "get_memories_by_ids")
            if method is None or not inspect.iscoroutinefunction(method):
                continue
            try:
                result = method(ids)
                if inspect.isawaitable(result):
                    rows = await result
                    if not isinstance(rows, list):
                        continue
                    # 可见性查询失败（fail-open 风险）时，本地兜底强制
                    # active + scope 过滤，绝不因回退路径放大可见集合。
                    if id(repository) in visibility_failed:
                        rows = [
                            row for row in rows
                            if row.get("status") == "active"
                        ]
                        if scope is not None:
                            rows = [
                                row for row in rows
                                if scope.matches_record(row)
                            ]
                    return rows
            except Exception as exc:
                logger.debug("memory.vector_legacy_backfill_failed", error=str(exc))

        fallback = [
            original_memories[memory_id]
            for memory_id in ids
            if memory_id in original_memories
        ]
        # 可见性仓储存在但查询失败时，本地兜底强制 active + scope 过滤。
        if visibility_capable:
            fallback = [
                row for row in fallback
                if row.get("status") == "active"
            ]
            if scope is not None:
                fallback = [
                    row for row in fallback if scope.matches_record(row)
                ]
        return fallback

    async def _hybrid_vec_search(self, query: str, k: int,
                                 candidate_ids: list[int] | None = None,
                                 is_raw: int | None = None,
                                 scope: Any | None = None,
                                 query_vec: list[float] | None = None) -> list[dict]:
        """向量检索 + 批量 JOIN：一次查询获取所有向量命中的记忆记录

        ContextNest A1: candidate_ids 提供时, 向量检索只在确定性候选集内排序,
        候选集本身由 metadata selector (时间/重要性) 产生, Jaccard 1.0。
        is_raw: None=不过滤, 0=只查蒸馏知识, 1=只查原始记忆
        scope: 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
        query_vec: P1-4 预计算查询向量（多查询场景批量 embed 后复用），None 时内部 embed
        """
        if not self._mm.vec:
            return []
        try:
            # 根因修复（2026-07-29）：移除外层 3.5s wait_for 超时（治标）。
            # embed client 已配 connect=15s + max_retries=0，vec.search 内部调 embed
            # （有 10s 单次超时 + 重试保护）+ 本地 sqlite_vec 搜索（毫秒级）。
            # 原 3.5s 超时在 embed 慢时必然先触发，导致向量通道被跳过 → "想不起来"。
            # 注：原注释"embed 6.9s 击穿 8s"的根因正是 connect=5s 过短，现已修复。
            __st = time.time()
            # P0 隐私（2026-08-24）：scoped 候选改为 recent-first keyset 分页逐页
            # 送入向量检索并跨页合并 top-N，替代旧的一次性候选收集：
            # (a) 预算（SCOPE_SCAN_PAGE_SIZE/MAX_PAGES/BUDGET_MS）内流式消费；
            # (b) 预算耗尽记 memory.scope_scan_partial 并保留已扫部分结果；
            # (c) scope=None 时单页直通，行为与旧实现一致。
            # HyDE（假设文档嵌入）：开启时生成假设答案文档，与原查询向量混合检索。
            # 默认关闭（HYDE_ENABLED=False），避免查询变换跑偏（同多查询扩展教训）。
            import config as _hyde_cfg
            _hyde_enabled = getattr(_hyde_cfg, "HYDE_ENABLED", False)
            _hyde_subset = getattr(_hyde_cfg, "HYDE_SUBSET_MODE", "off")
            _hyde_doc = None
            if (
                _hyde_enabled and self._mm._query_transformer
                and self._mm._query_transformer.available
                and (
                    _hyde_subset != "non_exact"
                    or self._mm._query_transformer.should_use_hyde(query)
                )
            ):
                try:
                    _hyde_doc = await (
                        self._mm._query_transformer.generate_hyde_document(query)
                    )
                except Exception as e:
                    logger.debug("memory.hyde_failed", error=str(e))
                    _hyde_doc = None
            vec_results: list[tuple[int, float]] = []
            original_memories: dict[int, dict] = {}
            seen_ids: set[int] = set()
            # 跨页合并维护全局 top-N（N=k）：每页只送当前页候选进向量检索，
            # 合并后截断，保证返回规模与单次 KNN 一致且不随页数膨胀。
            top_n = k
            async for page_candidate_ids in self._iter_visible_candidate_pages(
                scope, candidate_ids
            ):
                # None=无限制全库检索（scope=None 直通）；[]=本页无可见候选跳过
                page_list = (
                    None if page_candidate_ids is None
                    else list(page_candidate_ids)
                )
                if page_list == []:
                    continue
                if _hyde_doc:
                    page_raw = await self._mm.vec.search_with_hyde(
                        query, hyde_doc=_hyde_doc, alpha=0.4,
                        k=top_n, candidate_ids=page_list,
                    )
                    _stage_log("vec_embed_hyde_search", __st, query)
                else:
                    page_raw = await self._mm.vec.search(
                        query, top_k=top_n,
                        candidate_ids=page_list,
                        deterministic=True,
                        query_vec=query_vec,
                    )
                    _stage_log("vec_embed_search", __st, query)
                page_hits, page_originals = _normalize_vector_hits(page_raw)
                for row_id, original in page_originals.items():
                    original_memories.setdefault(row_id, original)
                _merge_vector_page_hits(vec_results, page_hits, seen_ids, top_n)
            if not vec_results:
                return []
            vec_ids = [row_id for row_id, _ in vec_results]
            vec_mems = await self._get_visible_vector_memories(
                vec_ids, scope, original_memories
            )
            if is_raw is not None:
                vec_mems = [m for m in vec_mems if m.get("is_raw") == is_raw]
            if scope is not None:
                vec_mems = [m for m in vec_mems if scope.matches_record(m)]
            # 构建 id -> memory 映射，按 distance 排序组装结果
            vec_mem_map = {m["id"]: m for m in vec_mems}

            # P0 分页合并（2026-08-24）：JOIN/后过滤会淘汰部分 top-N 候选
            # （不可见/superseded/is_raw 不匹配），按距离升序截断回 top-N，
            # 保证返回规模不随扫描页数膨胀，与单次 KNN 语义一致。
            ordered_ids = [
                row_id for row_id, _ in sorted(vec_results)
            ]
            if len(ordered_ids) > top_n:
                for row_id in ordered_ids[top_n:]:
                    vec_mem_map.pop(row_id, None)

            # 治本修复（TDD test_rag_quality_root_fix）：
            # 原 _hybrid_vec_search 用相对归一化 (1 - distance/max_dist) 美化距离，
            # 即使最远的向量也接近 1.0 高分，导致 Python query 召回亲密内容。
            # 改用绝对 L2 距离阈值：distance > RAG_VEC_MAX_DISTANCE 的向量直接丢弃，
            # 不进入 RRF 融合，从源头杜绝噪声。
            import config as _cfg
            _max_distance = getattr(_cfg, 'RAG_VEC_MAX_DISTANCE', 1.0)
            _soft_penalty = getattr(_cfg, 'RAG_VEC_SOFT_PENALTY', 0.3)
            _filtered_count = 0
            _demoted_count = 0
            items = []
            for row_id, distance in vec_results:
                mem = vec_mem_map.get(row_id)
                if mem:
                    # P0-1: 统一绝对相似度 (1 - distance)，去掉相对归一化 (1 - distance/max_dist)。
                    # 根因：相对归一化会把过滤后最大距离映射到 0.0、最小距离映射到 1.0，
                    # 即使所有结果都距离很远（接近 RAG_VEC_MAX_DISTANCE），最相关的也有高分，
                    # 与绝对阈值过滤配合时分数失真。绝对距离 0~1.0 映射相似度 1.0~0.0，
                    # 与 RAG_MIN_FINAL_SCORE / RRF 的分数语义对齐。
                    # P0-2: 硬阈值改软降权。distance > _max_distance 不再丢弃，
                    # 而是降权保留，避免语义查询整体偏远时向量通道空转。
                    # Reranker 仍可判定相关性，噪声由 final_score 最低分过滤兜底。
                    # P0-3 修复：原实现 sim = max(0, 1-dist) * penalty，当 dist>1.0 时
                    # sim=0，乘以任何 penalty 仍为 0，降权系数完全无效！
                    # 诊断："饮食偏好"→"不吃香菜" dist=1.19，sim=0，Reranker 无法捞回。
                    # 修复：超阈值时使用 (1 - dist/max_dist*1.2) * penalty 公式，
                    # 确保 sim > 0（即使 dist 略超 max_dist），Reranker 可正常排序。
                    if distance <= _max_distance:
                        sim = max(0.0, 1.0 - distance)
                    else:
                        _demoted_count += 1
                        # 超阈值软降权：用 (1 - dist/(max_dist*1.2)) * penalty
                        # 确保在 dist 略超 max_dist 时 sim 仍为正数。
                        # 例：max_dist=1.15, dist=1.19, penalty=0.5:
                        #   sim = (1 - 1.19/1.38) * 0.5 = (1-0.862) * 0.5 = 0.069
                        # Reranker 可基于此非零分排序，而非一律 0 分无法区分。
                        sim = max(0.01, (1.0 - distance / (_max_distance * 1.2))) * _soft_penalty
                    mem["score"] = sim
                    items.append(mem)
            if _demoted_count > 0:
                logger.info("memory.vec_distance_demoted",
                            query=query[:50],
                            total=len(vec_results),
                            demoted=_demoted_count,
                            kept=len(items),
                            max_distance=_max_distance)
            return items
        except Exception as e:
            from local_ai.integration.reranker import LocalModelUnavailableError

            if isinstance(e, LocalModelUnavailableError):
                raise
            logger.warning("memory.vec_search_failed", error=str(e))
            return []


    async def _spreading_recall(self, query: str, limit: int,
                                scope: Any | None = None) -> list[dict]:
        """扩散激活第五路检索通道

        通过 SpreadingActivationEngine 检索 concept_nodes，
        将结果映射回 episodic_memories（通过 source_mem_id）。
        scope 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。

        MEMORY_RETRIEVAL_DIFFUSION=False 时跳过扩散（精准检索），
        避免通过概念图找回应被艾宾浩斯遗忘曲线衰减归档的低 importance 记忆。
        """
        if not self._mm.spreading_engine:
            return []
        # 精准检索开关：False 时跳过概念图扩散
        import config
        if not getattr(config, "MEMORY_RETRIEVAL_DIFFUSION", False):
            return []
        try:
            candidate_ids = await self._get_visible_candidate_ids(scope)
            if candidate_ids == []:
                return []
            results = await self._mm.spreading_engine.recall(
                query,
                top_k=limit,
                candidate_ids=candidate_ids,
                cache_namespace=scope.cache_namespace() if scope is not None else "",
            )
            if not results:
                return []
            # 映射回 episodic_memories，多 node 指向同一 memory 时取最高分。
            # recall() 结果已携带 source_mem_id（alive_nodes 内存直读），
            # 逐条 get_node 是纯冗余 DB 往返（top_k=120 时最多 120 次串行
            # 查询挤占共享 aiosqlite 连接）；仅结果缺该字段时回退查库，
            # 兼容旧引擎/Mock。
            mem_ids = []
            for r in results:
                source_mem_id = r.get("source_mem_id")
                if source_mem_id is None:
                    node = await self._mm.spreading_engine.db.get_node(r["id"])
                    source_mem_id = node.get("source_mem_id") if node else None
                if source_mem_id:
                    mem_ids.append((source_mem_id, r["score"]))
            if not mem_ids:
                return []
            # 批量获取记忆
            ids = [m[0] for m in mem_ids]
            # 多 node 指向同一 memory 时保留最高分（取 max 而非覆盖）
            score_map: dict[int, float] = {}
            for mid, score in mem_ids:
                if mid not in score_map or score > score_map[mid]:
                    score_map[mid] = score
            memories = await self._mm.memory.get_visible_memories_by_ids(
                ids, scope=scope
            )
            if scope is not None:
                memories = [m for m in memories
                            if m.get("user_id") == scope.user_id
                            and m.get("agent_id") == scope.agent_id]
            for mem in memories:
                mem["spreading_score"] = score_map.get(mem["id"], 0.0)
                mem["spreading_recall"] = True
            return memories
        except Exception as e:
            logger.debug("memory.spreading_recall_failed", error=str(e))
            return []


    def _extract_deterministic_selectors(self, query: str) -> dict[str, Any]:
        """ContextNest A1: 从查询中提取确定性 selector (metadata-based, Jaccard 1.0)。

        与向量检索 (概率性, 论文实测 mean Jaccard 0.611) 互补:
        selector 先产生确定性候选集, 向量只在集内排序。

        Returns:
            dict 可选键:
            - time_range: (start_ts, end_ts) 来自"昨天/前天/上周"等时间词
            - min_importance: float  (当前留空, 由调用方按需填)
            - has_selectors: bool   是否有任何确定性 selector 可用
        """
        selectors: dict = {"has_selectors": False}
        try:
            tr = _parse_temporal_query(query)
            if tr:
                selectors["time_range"] = tr
                selectors["has_selectors"] = True
        except Exception as e:
            logger.debug("memory.selector_extract_failed", error=str(e))
        return selectors


    async def _get_candidate_ids_by_selectors(self, selectors: dict,
                                                limit: int = 200,
                                                scope: Any | None = None) -> list[int] | None:
        """根据确定性 selector 查询候选 rowid 集合。

        无 selector 返回 None (调用方走原 KNN 全量检索)。
        scope 非空时追加 user_id/agent_id 过滤，防止跨用户候选泄露。
        """
        if not selectors.get("has_selectors"):
            return None
        clauses: list[str] = []
        params: list = []
        if scope is not None:
            scope_where, scope_params = scope.to_sql_filter_parametrized("")
            clauses.append(f"({scope_where})")
            params.extend(scope_params)

        if "time_range" in selectors:
            s, e = selectors["time_range"]
            clauses.append("timestamp BETWEEN ? AND ?")
            params.extend([s, e])
        if "min_importance" in selectors:
            clauses.append("importance >= ?")
            params.append(selectors["min_importance"])
        from db.db_memory_utils import active_memory_visibility_sql
        clauses.append(active_memory_visibility_sql())
        # ORDER BY id 保证候选集本身有序确定
        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        try:
            cursor = await self._mm.memory._conn.execute(
                f"SELECT id FROM episodic_memories WHERE {where} "
                f"ORDER BY id LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows] if rows else []
        except Exception as e:
            logger.debug("memory.candidate_ids_failed", error=str(e))
            return None


    async def _try_temporal_search(self, query: str, k: int,
                                    scope: Any | None = None,
                                    include_raw: bool = False,
                                    conv_user_id: str = "") -> list[dict] | None:
        """时间型查询：直接查 conversation_logs 原始对话。

        根本修复：时间查询最需要的是完整的原始对话记录，不是经过 FTS/reranker/CRAG
        多层管线过滤后的蒸馏摘要。conversation_logs 是最可靠、最完整的数据源。

        查找顺序：conversation_logs → episodic_memories（兜底）
        无时间词返回 None（调用方继续走常规语义检索）。

        P0 修复：conv_user_id 非空时按 user_id 过滤，防止跨用户对话泄露。
        """
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        _time_range = _parse_temporal_query(query)
        if not _time_range:
            return None
        start_ts, end_ts = _time_range
        try:
            # 第一优先：直接查 conversation_logs 原始对话（最可靠）
            # 时间查询用户要的是"发生了什么"，原始对话比蒸馏摘要更准确
            _conv_results = await self._mm._search_conversation_logs(
                start_ts, end_ts, scope, k * 4, conv_user_id=conv_user_id)
            if _conv_results:
                logger.debug("memory.temporal_convlogs_hit",
                             query=query[:50], count=len(_conv_results))
                return _conv_results

            # 兜底：conversation_logs 无结果时查 episodic_memories
            # （可能对话还没来得及记录，但蒸馏记忆已生成）
            is_raw_filter = None if include_raw else 0
            _time_results = await self._mm.memory.search_memories_by_time_scoped(
                start_ts, end_ts, scope=scope, limit=k * 2, is_raw=is_raw_filter
            )
            if _time_results:
                logger.debug("memory.temporal_episodic_hit",
                             query=query[:50], count=len(_time_results))
                return _time_results
            # 两级 fallback：含 is_raw=1 的原始记录
            if is_raw_filter is not None:
                _fallback_results = await self._mm.memory.search_memories_by_time_scoped(
                    start_ts, end_ts, scope=scope, limit=k * 2, is_raw=None
                )
                if _fallback_results:
                    logger.debug("memory.temporal_fallback_raw_hit",
                                 query=query[:50], count=len(_fallback_results))
                    return _fallback_results
            return []
        except Exception as e:
            logger.warning("memory.temporal_search_failed", error=str(e))
            return None


    async def _search_conversation_logs(self, start_ts: float, end_ts: float,
                                         scope: Any | None, k: int,
                                         conv_user_id: str = "") -> list[dict]:
        """查 conversation_logs 原始对话，格式化为记忆格式返回。

        P0 修复（上下文污染根因）：按 conv_user_id 过滤。
        根因：原实现不过滤 user_id，导致其他用户/会话的原始对话被注入当前上下文。
              用户反馈"那是之前的数据库里面的原文直接蹦出来了"——AI 看到了不属于
              当前用户的对话记录，导致上下文混乱、重复回复、角色出戏。
        修复：conv_user_id 非空时按 user_id 过滤，仅返回当前用户的对话。
              conv_user_id 为空时保留原行为（向后兼容，但不应在新代码中使用）。
        """
        try:
            # P0 修复：按 conv_user_id 过滤，防止跨用户对话泄露
            raw = await self._mm.memory.get_conversations_by_time_range(
                start_ts, end_ts, user_id=conv_user_id, limit=k, scope=scope
            )
            if not raw:
                return []
            results = []
            for row in raw:
                ts = row.get("timestamp", 0)
                user_msg = (row.get("user_message") or "")
                asst_msg = (row.get("assistant_reply") or "")
                if not user_msg and not asst_msg:
                    continue
                # 场景指令检测：用户有时发送"（场景：...格式：...）"这类
                # 元指令来控制 agent 行为，不是真正的对话内容。LLM 在回忆时
                # 会原样复述这些指令（系统 prompt 泄漏），所以需要标记为
                # "场景指令"，让 LLM 知道这不是需要复述给用户听的内容。
                if user_msg.startswith("（场景：") or user_msg.startswith("(场景："):
                    user_msg = "（场景指令，非对话内容，回忆时不要复述）"
                # 带完整日期的时间锚点 + 叙事化格式：根因修复
                # 之前格式"时间：...\n爸爸：...\n小妲：..."像数据记录，LLM 模仿输出
                # "时间线整理：⏰ 约7:09"等出戏格式。改为叙事性格式——像回忆的画面
                # 浮现，而不是日志条目。LLM 看到叙事性内容，回忆时也会用叙事性语言。
                # 同时带完整年月日，防止 LLM 被记忆内容里的日期干扰（如用户当时
                # 在回忆"7月16日"，LLM 会采用内容里的日期作为锚点）。
                if ts:
                    from datetime import datetime as _dt_cls
                    _dt = _dt_cls.fromtimestamp(float(ts))
                    _period = _natural_time_desc(float(ts))
                    time_str = f"{_dt.year}年{_dt.month}月{_dt.day}日{_period}"
                else:
                    time_str = "某时"
                # 叙事化：用"——"连接时间和对话，用"爸爸说""你回答"代替"爸爸：""小妲："
                # 这种格式让 LLM 觉得这是回忆片段，不是数据记录
                summary = f"{time_str}——\n爸爸说：{user_msg}"
                if asst_msg:
                    summary += f"\n你当时回答：{asst_msg}"
                results.append({
                    "summary": summary,
                    "timestamp": ts,
                    "importance": 0.5,
                    "type": "conversation_log",
                    "is_raw": 1,
                    "user_id": scope.user_id if scope else "",
                    "agent_id": scope.agent_id if scope else "",
                    "session_id": row.get("session_id", ""),
                })
            return results[:k]
        except Exception as e:
            logger.warning("memory.convlogs_search_failed", error=str(e))
            return []


    async def _vector_fallback_search(self, query: str, k: int,
                                       scope: Any | None = None) -> list[dict]:
        """降级：纯向量检索 + 批量 JOIN。

        scope 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
        """
        results = await self._hybrid_vec_search(query, k, scope=scope)
        return results[:k]
