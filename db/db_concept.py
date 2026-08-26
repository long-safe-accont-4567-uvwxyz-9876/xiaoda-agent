"""概念图数据库 CRUD — concept_nodes / concept_edges / concept_meta 表操作"""
import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    """返回 Asia/Shanghai 时区的 ISO 时间戳"""
    return datetime.now(_SH_TZ).isoformat()


class ConceptDB:
    """概念图数据库访问层（异步 aiosqlite）"""

    # alive_nodes 全表读的 TTL 快照（2026-08-25 性能专项：USB 盘冷读实测 1.7s）
    _ALIVE_NODES_TTL_S = 60.0
    # 结构性变更才失效缓存；纯统计字段（weight/access_count 等）不失效，
    # 否则每次检索后的 touch 批量更新会把命中率打没（与图快照 TTL 同一取舍）
    _STRUCTURAL_NODE_FIELDS = frozenset(
        {"text", "keys", "valid_to", "superseded_by", "layer"})
    # 建边 key 的文档频率（DF）上限：存活节点中占比超过该值的 key 视为
    # 无区分度的高扩散词（如会话角色词"爸爸/小妲/用户"，实测 DF 59%~77%，
    # 曾致 2463 节点互连出 100 万条 co-occurrence 边）。与
    # scripts/prune_concept_edges.py 的剪枝阈值保持一致。
    MAX_KEY_DF_RATIO = 0.05

    def __init__(self, conn):
        self._conn = conn
        self._alive_cache: dict[str, dict] | None = None
        self._alive_cache_ts: float = 0.0
        self._stopkey_cache: set[str] | None = None

    def _invalidate_alive_nodes_cache(self) -> None:
        self._alive_cache = None
        self._alive_cache_ts = 0.0
        self._stopkey_cache = None

    async def _get_stopkeys(self) -> set[str]:
        """计算高扩散 key 集合（DF > MAX_KEY_DF_RATIO 的存活节点 key）。

        结果随 alive_nodes 缓存生命周期失效；alive 快照为空（冷启动/测试）
        时返回空集——节点数少时 DF 天然高，不应拦建边。
        """
        if self._stopkey_cache is not None:
            return self._stopkey_cache
        alive = await self.get_alive_nodes()
        if len(alive) < 50:
            self._stopkey_cache = set()
            return self._stopkey_cache
        df: dict[str, int] = {}
        for node in alive.values():
            try:
                for k in set(json.loads(node.get("keys", "[]"))):
                    df[k] = df.get(k, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue
        threshold = max(1, int(len(alive) * self.MAX_KEY_DF_RATIO))
        self._stopkey_cache = {k for k, v in df.items() if v > threshold}
        if self._stopkey_cache:
            logger.info("concept_db.stopkeys_computed",
                        count=len(self._stopkey_cache),
                        sample=sorted(self._stopkey_cache)[:8])
        return self._stopkey_cache

    async def insert_node(self, id: str, text: str, keys: str,
                          weight: float = 1.0, peak_weight: float = 1.0,
                          confidence: float = 1.0, access_count: int = 0,
                          layer: str = "hippocampus",
                          created: str | None = None,
                          last_accessed: str | None = None,
                          valid_from: str | None = None,
                          valid_to: str | None = None,
                          superseded_by: str | None = None,
                          history: str = "[]",
                          origin: str = "{}",
                          source_mem_id: int | None = None,
                          embedding=None,
                          difficulty: float = 5.0,
                          stability: float = 3.0,
                          phase: str = "buffer",
                          last_review: float = 0.0,
                          reinforcement_count: int = 0,
                          auto_commit: bool = True) -> None:
        """插入概念节点。keys 为 JSON 字符串。使用 UPSERT 避免覆盖已有 FSRS 状态。"""
        self._invalidate_alive_nodes_cache()
        now = created or _now_iso()
        await self._conn.execute(
            """INSERT INTO concept_nodes
               (id, text, weight, peak_weight, confidence, access_count, keys,
                layer, created, last_accessed, valid_from, valid_to,
                superseded_by, history, origin, source_mem_id, embedding,
                difficulty, stability, phase, last_review, reinforcement_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   text = excluded.text,
                   weight = excluded.weight,
                   peak_weight = excluded.peak_weight,
                   confidence = excluded.confidence,
                   access_count = excluded.access_count,
                   keys = excluded.keys,
                   layer = excluded.layer,
                   last_accessed = excluded.last_accessed,
                   valid_from = excluded.valid_from,
                   valid_to = excluded.valid_to,
                   superseded_by = excluded.superseded_by,
                   history = excluded.history,
                   origin = excluded.origin,
                   embedding = excluded.embedding,
                   source_mem_id = CASE
                       WHEN excluded.source_mem_id IS NOT NULL THEN excluded.source_mem_id
                       ELSE concept_nodes.source_mem_id
                   END,
                   difficulty = CASE
                       WHEN excluded.difficulty != 5.0 OR concept_nodes.difficulty IS NULL THEN excluded.difficulty
                       ELSE concept_nodes.difficulty
                   END,
                   stability = CASE
                       WHEN excluded.stability != 3.0 OR concept_nodes.stability IS NULL THEN excluded.stability
                       ELSE concept_nodes.stability
                   END,
                   phase = CASE
                       WHEN excluded.phase != 'buffer' OR concept_nodes.phase IS NULL THEN excluded.phase
                       ELSE concept_nodes.phase
                   END,
                   last_review = CASE
                       WHEN excluded.last_review > 0 THEN excluded.last_review
                       ELSE concept_nodes.last_review
                   END,
                   reinforcement_count = CASE
                       WHEN excluded.reinforcement_count > 0 THEN excluded.reinforcement_count
                       ELSE concept_nodes.reinforcement_count
                   END""",
            (id, text, weight, peak_weight, confidence, access_count, keys,
             layer, now, last_accessed or now, valid_from or now, valid_to,
             superseded_by, history, origin, source_mem_id, embedding,
             difficulty, stability, phase, last_review, reinforcement_count),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_node(self, node_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE id = ?", (node_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_node_by_source_mem(self, mem_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE source_mem_id = ?", (mem_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_node(self, node_id: str, auto_commit: bool = True, **fields) -> None:
        if not fields:
            return
        if not self._STRUCTURAL_NODE_FIELDS.isdisjoint(fields):
            self._invalidate_alive_nodes_cache()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [node_id]
        await self._conn.execute(
            f"UPDATE concept_nodes SET {cols} WHERE id = ?", vals
        )
        if auto_commit:
            await self._conn.commit()

    async def get_alive_nodes(self, limit: int = 0, offset: int = 0) -> dict[str, dict]:
        """返回有效节点（valid_to IS NULL），支持分页。limit=0 表示不分页。

        全量读取（limit=0 且 offset=0）走 TTL 快照：扩散召回每轮全表拉取，
        USB 盘冷读实测 1.7s。返回浅拷贝防止调用方修改污染缓存。
        结构性写入（insert/valid_to/supersede/text/keys 变更）即时失效；
        纯统计字段更新容忍 ≤TTL 的陈旧（与图边快照同一取舍）。
        """
        if limit > 0 or offset > 0:
            async with self._conn.execute(
                "SELECT * FROM concept_nodes WHERE valid_to IS NULL LIMIT ? OFFSET ?",
                (limit, offset)
            ) as cur:
                rows = await cur.fetchall()
            return {row["id"]: dict(row) for row in rows}

        now = time.monotonic()
        if (self._alive_cache is not None
                and now - self._alive_cache_ts < self._ALIVE_NODES_TTL_S):
            logger.debug("concept.alive_cache_hit rows={}",
                         len(self._alive_cache))
            return {nid: dict(node) for nid, node in self._alive_cache.items()}

        _t0 = time.monotonic()
        async with self._conn.execute(
            "SELECT * FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            rows = await cur.fetchall()
        result = {row["id"]: dict(row) for row in rows}
        self._alive_cache = result
        self._alive_cache_ts = now
        logger.debug("concept.alive_cache_miss rows={} took_ms={}",
                     len(result), int((time.monotonic() - _t0) * 1000))
        return {nid: dict(node) for nid, node in result.items()}

    async def get_node_count(self) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def create_edge(self, source_id: str, target_id: str,
                           relation: str = "related", weight: float = 1.0,
                           created: str | None = None,
                           auto_commit: bool = True) -> None:
        now = created or _now_iso()
        await self._conn.execute(
            """INSERT OR REPLACE INTO concept_edges
               (source_id, target_id, relation, weight, created)
               VALUES (?, ?, ?, ?, ?)""",
            (source_id, target_id, relation, weight, now),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_edges(self, node_id: str) -> dict[str, dict]:
        async with self._conn.execute(
            "SELECT * FROM concept_edges WHERE source_id = ?", (node_id,)
        ) as cur:
            rows = await cur.fetchall()
            return {row["target_id"]: dict(row) for row in rows}

    async def get_edge_snapshot(self) -> dict[str, dict[str, float]]:
        """一次取回全部概念边（{source_id: {target_id: weight}}）。

        扩散激活在稠密图（实测 2381 节点 / 100 万边）逐节点或按 hop 批量
        取边都要反复拉回巨量行；SpreadingActivationEngine 把整图快照缓存
        在内存，写入路径经 clear_cache() 失效重建。
        """
        async with self._conn.execute(
            "SELECT source_id, target_id, weight FROM concept_edges"
        ) as cur:
            rows = await cur.fetchall()
        # 100 万行的 dict 组装是纯 CPU 密集（首载实测 3-4s），且 aiosqlite 仅
        # 负责取行、组装回到事件循环线程执行——移线程池避免冻结主线程
        # （检索方通常有 10-45s 的超时预算，但冻结事件循环是全服务级事故）。
        def _build_snapshot() -> dict[str, dict[str, float]]:
            result: dict[str, dict[str, float]] = {}
            for row in rows:
                result.setdefault(row["source_id"], {})[
                    row["target_id"]
                ] = float(row["weight"])
            return result

        return await asyncio.to_thread(_build_snapshot)

    async def update_edge(self, source_id: str, target_id: str,
                           weight: float | None = None,
                           relation: str | None = None,
                           auto_commit: bool = True) -> None:
        fields = {}
        if weight is not None:
            fields["weight"] = weight
        if relation is not None:
            fields["relation"] = relation
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [source_id, target_id]
        await self._conn.execute(
            f"UPDATE concept_edges SET {cols} WHERE source_id = ? AND target_id = ?",
            vals,
        )
        if auto_commit:
            await self._conn.commit()

    async def auto_link(self, node_id: str, keys: list[str],
                         min_shared: int = 3) -> int:
        """与共享 ≥ min_shared 个 keys 的存活节点自动建边。返回建边数。

        性能保护：
        1. 当存活节点 > 200 时跳过实时建边（遍历 1400+ 节点的 JSON 解析 +
           集合交集是纯 CPU 操作，会阻塞事件循环 5-15s，且 asyncio.wait_for
           无法中断无 await 点的 CPU 循环）。改由后台 curator 任务批量补建。
        2. CPU 匹配逻辑移至线程池（asyncio.to_thread），避免阻塞事件循环。
        """
        if not keys:
            return 0
        # 性能保护：节点过多时跳过，避免阻塞事件循环
        node_count = await self.get_node_count()
        if node_count > 200:
            logger.info("concept_graph.auto_link_skipped",
                        alive_nodes=node_count,
                        hint="存活节点 >200，跳过实时建边，由后台 curator 补建")
            return 0
        alive = await self.get_alive_nodes()
        # CPU 匹配逻辑移至线程池，避免 json.loads x N 阻塞事件循环
        key_set = set(keys)
        # 高扩散 key 过滤（防复发 2026-08-27）：角色词等 DF>5% 的 key 无区分度，
        # 不过滤会退化为近稠密图（历史事故：100 万条 co-occurrence 边占库 3/4）
        key_set = key_set - await self._get_stopkeys()
        if not key_set:
            return 0
        match_pairs = await asyncio.to_thread(
            self._compute_link_pairs, alive, node_id, key_set, min_shared)
        if not match_pairs:
            return 0
        now = _now_iso()
        for nid in match_pairs:
            await self.create_edge(node_id, nid, "co-occurrence", 1.0, now, auto_commit=False)
            await self.create_edge(nid, node_id, "co-occurrence", 1.0, now, auto_commit=False)
        await self._conn.commit()
        return len(match_pairs)

    @staticmethod
    def _compute_link_pairs(alive: dict, node_id: str,
                            key_set: set, min_shared: int) -> list[str]:
        """纯 CPU 计算：遍历存活节点，返回共享 ≥ min_shared keys 的节点 ID 列表。

        在线程池中执行，不阻塞事件循环。
        """
        matches: list[str] = []
        for nid, node in alive.items():
            if nid == node_id:
                continue
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                continue
            shared = key_set & node_keys
            if len(shared) >= min_shared:
                matches.append(nid)
        return matches

    async def batch_link_recent(self, batch_size: int = 50,
                                 min_shared: int = 3,
                                 max_per_node: int = 20,
                                 max_edges_per_run: int = 60,
                                 auto_commit: bool = True) -> int:
        """后台 curator：为最近创建的、尚无边的节点批量补建边。

        auto_link 在存活节点 >200 时会跳过实时建边，由本方法在后台补建。
        每次最多处理 batch_size 个无边节点，避免长时间占用 DB 连接。

        Args:
            max_per_node: 每个节点最多补建的边数（防止热点节点连接过多）
            max_edges_per_run: 单次 curator 最多写入的边数（N10 修复：
                限制单次 I/O 占用，避免在 USB 盘上长时间写入饿死 recall
                等用户路径的 aiosqlite 查询）

        Returns:
            补建的边数
        """
        # CodeRabbit #7：参数校验——拒绝负数和布尔值，避免 LIMIT/切片异常
        for _name, _val in (("batch_size", batch_size), ("min_shared", min_shared),
                            ("max_per_node", max_per_node),
                            ("max_edges_per_run", max_edges_per_run)):
            if _val is None or (not isinstance(_val, int) or isinstance(_val, bool)) or _val < 0:
                raise ValueError(f"{_name} 必须非负整数，得到 {_val}")
        # CodeRabbit #3：max_edges_per_run=0 时不查询不计算，直接返回 0
        if max_edges_per_run == 0:
            return 0

        # 1. 找出最近创建的、无边节点
        async with self._conn.execute(
            """SELECT cn.id, cn.keys FROM concept_nodes cn
               WHERE cn.valid_to IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM concept_edges ce WHERE ce.source_id = cn.id
                 )
               ORDER BY cn.created DESC LIMIT ?""",
            (batch_size,)
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return 0

        # 2. 获取存活节点的 keys 映射（仅 id + keys，减少内存）
        async with self._conn.execute(
            "SELECT id, keys FROM concept_nodes WHERE valid_to IS NULL"
        ) as cur:
            all_rows = await cur.fetchall()

        # 3. CPU 匹配移至线程池
        target_list = [(r["id"], r["keys"]) for r in rows]
        all_keys_map = {r["id"]: r["keys"] for r in all_rows}
        # 高扩散 key 过滤（防复发 2026-08-27）：与 auto_link 同一阈值，
        # 防止角色词让 curator 把图再次推向稠密
        stopkeys = await self._get_stopkeys()
        if stopkeys:
            all_keys_map = {
                nid: json.dumps(sorted(
                    (set(json.loads(ks or "[]")) - stopkeys)
                    if ks else set()))
                for nid, ks in all_keys_map.items()
            }
        edge_pairs = await asyncio.to_thread(
            self._compute_batch_links, target_list, all_keys_map, min_shared,
            max_per_node)

        if not edge_pairs:
            return 0

        # N10 修复：单次写入边数上限，避免在 USB 盘上长时间顺序写入饿死
        # recall 等用户路径。60 条边 × 2（双向）= 120 次 INSERT，在 USB 上约
        # 2-3s，远低于 30s 超时，给用户路径留足 DB 带宽。
        # CodeRabbit #7：max_edges_per_run 计数逻辑链接（一条无向边 = 2 次有向
        # INSERT）。_compute_batch_links 已按 (nid,other),(other,nid) 双向交替
        # 展开，故按 2 行/链接换算后切片，保证永远在完整逻辑链接边界切断，
        # 不会出现单向边。原代码直接切 [:max_edges_per_run] 会切到奇数行且
        # 实际只写 30 链接（与文档承诺的 60 链接=120 INSERT 不符）。
        max_rows = max_edges_per_run * 2
        if len(edge_pairs) > max_rows:
            edge_pairs = edge_pairs[:max_rows]

        # 4. 批量写入边（N10 修复：用 executemany 替代顺序 create_edge）
        # 原实现 `for src, tgt in edge_pairs: await self.create_edge(...)`
        # 每条边一次 await + 一次 execute，N 条边 = N 次 RPC 往返。
        # executemany 单次 RPC 提交全部边，I/O 占用从 N×latency 降到 1×latency。
        now = _now_iso()
        rows_to_insert = [
            (src, tgt, "co-occurrence", 1.0, now)
            for src, tgt in edge_pairs
        ]
        await self._conn.executemany(
            """INSERT OR REPLACE INTO concept_edges
               (source_id, target_id, relation, weight, created)
               VALUES (?, ?, ?, ?, ?)""",
            rows_to_insert,
        )
        if auto_commit:
            await self._conn.commit()
        # CodeRabbit #2: 返回逻辑链接数（一条无向边 = 2 行有向 INSERT），
        # 与文档"补建的边数"语义一致，而非有向行数
        return len(edge_pairs) // 2

    @staticmethod
    def _compute_batch_links(target_list: list, all_keys_map: dict,
                             min_shared: int, max_per_node: int = 20) -> list:
        """纯 CPU：为每个目标节点找出共享 ≥ min_shared keys 的节点对。

        CodeRabbit 复审修复：
        - emit 双向边 (nid, other_nid) 和 (other_nid, nid)，匹配 auto_link 的双向行为
        - 添加 max_per_node 参数，每个节点最多 max_per_node 条边，防止热点节点
        - CodeRabbit #1: 无向对去重——当 A、B 都在 target_list 时，处理 A 遇到 B
          与处理 B 遇到 A 会产生重复的双向对，用 seen_pairs 跳过已处理的无向对，
          避免 edge_pairs 膨胀挤占 max_edges_per_run 配额及 max_per_node 双倍消耗。
        """
        pairs = []
        node_edge_count: dict = {}  # 跟踪每个节点的边数
        seen_pairs: set = set()  # 无向对去重：frozenset({A, B})
        # 预解析所有 keys
        parsed = {}
        for nid, keys_str in all_keys_map.items():
            try:
                parsed[nid] = set(json.loads(keys_str or "[]"))
            except (json.JSONDecodeError, TypeError):
                parsed[nid] = set()

        for nid, keys_str in target_list:
            try:
                key_set = set(json.loads(keys_str or "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            if not key_set:
                continue
            for other_nid, other_keys in parsed.items():
                if other_nid == nid:
                    continue
                # CodeRabbit #1: 无向对去重——(A,B) 与 (B,A) 视为同一逻辑链接
                _pair_key = frozenset({nid, other_nid})
                if _pair_key in seen_pairs:
                    continue
                # 检查两个节点是否都未达到 max_per_node 上限
                if node_edge_count.get(nid, 0) >= max_per_node:
                    break  # nid 已达上限，停止为其找更多边
                if node_edge_count.get(other_nid, 0) >= max_per_node:
                    continue  # other_nid 已达上限，跳过
                if len(key_set & other_keys) >= min_shared:
                    seen_pairs.add(_pair_key)
                    # CodeRabbit: emit 双向边，匹配 auto_link 的双向行为
                    pairs.append((nid, other_nid))
                    pairs.append((other_nid, nid))
                    node_edge_count[nid] = node_edge_count.get(nid, 0) + 1
                    node_edge_count[other_nid] = node_edge_count.get(other_nid, 0) + 1
        return pairs

    async def get_meta(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM concept_meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO concept_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()
