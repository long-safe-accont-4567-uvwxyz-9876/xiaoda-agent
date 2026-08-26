"""冻结检索评测集 × 完整 RetrievalEngine 管线的适配器辅助模块。

供 tests/test_retrieval_private_dataset.py 使用，职责：

- PipelineRetrievalAdapter：评测接缝（app.state.core.memory）适配器，把
  /retrieval/evaluate 的检索调用接入完整管线——MemoryManager.retrieve_memories
  （七路召回 / RRF 融合 / score_kind / FSRS / query_cache），取代旧版裸调
  search_memories_fts_scoped 的假管线。
- HashingEmbedder / FakeVectorStore：确定性字符 bigram 哈希向量 stub，让向量通道
  非空、从而真正走 RRF 多路融合；纯词法信号零语义知识，不会把答案编码进夹具。
- SUPPLEMENTAL_FIXTURES：冻结数据集 executable_gate=false 案例的补充证据行
  （数据集本身未携带这些 evidence 夹具）；分级期望仍以冻结数据集为准。
- EXECUTION_PLAN：逐案例登记「管线执行」或「显式跳过原因」。无 LLM 环境无法公平
  执行的类别必须登记 skip_reason 并在报告中计数上报，禁止静默假绿。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable

from memory.scope import Scope


class PipelineHostUnavailable(RuntimeError):
    """完整检索管线在当前环境不可用（导入/构造失败），调用方应 skip-with-reason。"""


@dataclass(frozen=True)
class SupplementalFixture:
    evidence_id: str
    summary: str
    user_id: str = "eval-alice"
    agent_id: str = "xiaoda"
    session_id: str = "user"
    age_seconds: float = 0.0


SUPPLEMENTAL_FIXTURES: tuple[SupplementalFixture, ...] = (
    SupplementalFixture(
        "M:plc-decision:1",
        "PLC 编程助手方案评审会最后定了：采用事件驱动架构，由后端组负责落地",
    ),
    SupplementalFixture("M:roommate:city", "大学室友老周毕业后去了杭州定居"),
    SupplementalFixture(
        "M:roommate:job", "大学室友老周现在在杭州一家互联网公司做后端开发工作"
    ),
    SupplementalFixture(
        "M:coffee:v1", "用户以前喝咖啡喜欢加糖的拿铁", age_seconds=30 * 86400
    ),
    SupplementalFixture(
        "M:coffee:v2", "用户现在的咖啡偏好改成了无糖美式，不再喝拿铁"
    ),
    SupplementalFixture(
        "KG:group-a:activity",
        "群活动安排定了：本周六上午东湖绿道骑行",
        session_id="qq_group:eval-alice:group-a",
    ),
    SupplementalFixture(
        "KG:group-b:activity",
        "群活动安排定了：下周日爬山",
        session_id="qq_group:eval-alice:group-b",
    ),
)

SUPPLEMENTAL_BY_ID = {fixture.evidence_id: fixture for fixture in SUPPLEMENTAL_FIXTURES}


@dataclass(frozen=True)
class CaseExecutionPlan:
    case_id: str
    category: str
    executable: bool
    skip_reason: str = ""


EXECUTION_PLAN: dict[str, CaseExecutionPlan] = {
    "exact-identifier-001": CaseExecutionPlan(
        "exact-identifier-001", "exact_identifier", True),
    "semantic-preference-001": CaseExecutionPlan(
        "semantic-preference-001", "semantic_rewrite", False,
        "期望证据（忌口→不吃香菜）与查询无词法重叠，需真实语义嵌入或 LLM 查询改写"
        "才能公平执行；伪造语义向量等价于把答案写进夹具。仅保留 schema 校验。"),
    "coreference-001": CaseExecutionPlan(
        "coreference-001", "coreference", True),
    "temporal-hours-001": CaseExecutionPlan(
        "temporal-hours-001", "temporal", False,
        "查询『三小时前』是中文数字，_parse_temporal_query 仅识别阿拉伯数字"
        "（N小时前），时间通道在无 LLM 环境无法被该案例触发。仅保留 schema 校验。"),
    "negation-current-001": CaseExecutionPlan(
        "negation-current-001", "negation_and_current_fact", False,
        "需要 KG 事实的版本时序推理（KG:residence:v1/v2 + 当前态否定），KG 构建依赖"
        " LLM 实体抽取，本环境 KG 通道恒为空。仅保留 schema 校验。"),
    "multihop-001": CaseExecutionPlan(
        "multihop-001", "multi_hop", True),
    "conflict-001": CaseExecutionPlan(
        "conflict-001", "conflict", True),
    "unanswerable-001": CaseExecutionPlan(
        "unanswerable-001", "unanswerable", True),
    "scope-isolation-001": CaseExecutionPlan(
        "scope-isolation-001", "scope_isolation", True),
    "mixed-code-001": CaseExecutionPlan(
        "mixed-code-001", "mixed_zh_code", True),
    "group-scope-001": CaseExecutionPlan(
        "group-scope-001", "group_scope_isolation", True),
    "typo-alias-001": CaseExecutionPlan(
        "typo-alias-001", "typo_and_alias", False,
        "『川采馆』错别字与目标词无共享 jieba 词元、字符 bigram 也不相交，FTS 与哈希"
        "向量均无模糊匹配能力，需 LLM 别名归一化。仅保留 schema 校验。"),
    "exact-identifier-002": CaseExecutionPlan(
        "exact-identifier-002", "exact_identifier", True),
    "coreference-002": CaseExecutionPlan(
        "coreference-002", "coreference", True),
    "multihop-002": CaseExecutionPlan(
        "multihop-002", "multi_hop", True),
    "conflict-002": CaseExecutionPlan(
        "conflict-002", "conflict", True),
    "scope-isolation-002": CaseExecutionPlan(
        "scope-isolation-002", "scope_isolation", True),
    "mixed-code-002": CaseExecutionPlan(
        "mixed-code-002", "mixed_zh_code", True),
    "group-scope-002": CaseExecutionPlan(
        "group-scope-002", "group_scope_isolation", True),
    "unanswerable-002": CaseExecutionPlan(
        "unanswerable-002", "unanswerable", True),
}

EXECUTED_CASE_IDS: list[str] = [
    plan.case_id for plan in EXECUTION_PLAN.values() if plan.executable
]
SKIPPED_CASE_IDS: list[str] = [
    plan.case_id for plan in EXECUTION_PLAN.values() if not plan.executable
]


class HashingEmbedder:
    """确定性字符 bigram 哈希嵌入（固定维度 + L2 归一化），纯词法信号。"""

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def embed_text(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        lowered = text.lower()
        for i in range(len(lowered) - 1):
            gram = lowered[i:i + 2]
            digest = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
            vector[digest % self.dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class FakeVectorStore:
    """内存余弦向量库 stub：实现 _hybrid_vec_search 所依赖的最小接口。

    覆盖 channels.py 契约面：enabled / search(query, top_k, candidate_ids,
    deterministic, query_vec) / embed(texts)；另提供 search_child /
    search_with_hyde 空实现防止属性缺失异常。index_memory 是夹具写入入口，
    不属于生产 VectorStore 接口。
    """

    def __init__(self, embedder: HashingEmbedder | None = None) -> None:
        self.enabled = True
        self._embedder = embedder or HashingEmbedder()
        self._rows: dict[int, list[float]] = {}

    async def index_memory(self, row_id: int, text: str) -> None:
        self._rows[int(row_id)] = self._embedder.embed_text(text)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embedder.embed_text(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embedder.embed_text(text)

    async def search(self, query: str, top_k: int = 10,
                     candidate_ids: Any | None = None,
                     deterministic: bool = False,
                     query_vec: list[float] | None = None,
                     **_kwargs: Any) -> list[dict]:
        query_vector = query_vec if query_vec is not None else await self.embed_one(query)
        scored: list[dict] = []
        for row_id, row_vector in self._rows.items():
            if candidate_ids is not None and row_id not in candidate_ids:
                continue
            distance = 1.0 - sum(
                a * b for a, b in zip(query_vector, row_vector))
            scored.append({"rowid": row_id, "distance": distance})
        scored.sort(key=lambda hit: (hit["distance"], hit["rowid"]))
        return scored[:top_k]

    async def search_child(self, _query_vector: list[float], top_k: int = 10) -> list[dict]:
        return []

    async def search_with_hyde(self, query: str, hyde_doc: str | None = None,
                               alpha: float = 0.4, k: int = 10,
                               candidate_ids: Any | None = None,
                               **_kwargs: Any) -> list[dict]:
        return await self.search(query, top_k=k, candidate_ids=candidate_ids)


@dataclass
class RetrievalPipelineHost:
    """一次评测运行的管线宿主集合。"""

    manager: Any
    memory_manager: Any
    vector_store: FakeVectorStore


def build_retrieval_host(manager: Any) -> RetrievalPipelineHost:
    """基于真实 DatabaseManager 构造 MemoryManager + RetrievalEngine 宿主。

    概念图/扩散引擎置 None 以保证无 LLM 环境下的确定性（扩散通道默认关闭且
    非本评测的目标机制）；向量通道由 FakeVectorStore 提供，使七路召回中的
    FTS+Vec 双路非空、真正进入 RRF 融合与 score_kind=rrf 打分链路。
    """
    try:
        from memory.memory_manager import MemoryManager
        from memory.query_cache import QueryCache
        from memory.retrieval.pipeline import RetrievalEngine
    except Exception as exc:  # noqa: BLE001 — 任何导入失败都视为管线不可用
        raise PipelineHostUnavailable(f"检索管线导入失败: {exc}") from exc
    try:
        memory_manager = MemoryManager(manager, manager.memory)
    except Exception as exc:  # noqa: BLE001
        raise PipelineHostUnavailable(f"MemoryManager 构造失败: {exc}") from exc

    memory_manager.concept_graph = None
    memory_manager.spreading_engine = None
    memory_manager.confirm_correct = None
    vector_store = FakeVectorStore(HashingEmbedder())
    memory_manager.vec = vector_store
    memory_manager._query_cache = QueryCache(
        embed_func=vector_store.embed_one,
        threshold=0.97,
        max_size=64,
        ttl=300,
    )
    engine = getattr(memory_manager, "_retrieval", None)
    if not isinstance(engine, RetrievalEngine):
        raise PipelineHostUnavailable(
            "MemoryManager._retrieval 不是 RetrievalEngine 实例，完整管线不可用")
    return RetrievalPipelineHost(
        manager=manager, memory_manager=memory_manager, vector_store=vector_store)


async def insert_evaluation_fixtures(host: RetrievalPipelineHost,
                                     dataset: dict) -> dict[str, int]:
    """插入冻结夹具 + 已执行案例引用到的全部补充夹具，返回 stable_id → rowid。"""
    memory_repo = host.manager.memory
    frozen = {f["evidence_id"]: f for f in dataset["evidence_fixtures"]}
    needed: set[str] = set(frozen)
    cases_by_id = {case["id"]: case for case in dataset["cases"]}
    for case_id in EXECUTED_CASE_IDS:
        case = cases_by_id[case_id]
        needed.update(case.get("expect_relevance", {}))
        needed.update(case.get("forbidden_evidence_ids", []))
    missing = needed - set(frozen) - set(SUPPLEMENTAL_BY_ID)
    if missing:
        raise AssertionError(
            f"案例引用的证据缺少夹具定义（冻结集与补充集都没有）: {sorted(missing)}")

    stable_to_row: dict[str, int] = {}
    for evidence_id in sorted(needed):
        if evidence_id in frozen:
            summary = frozen[evidence_id]["summary"]
            scope_fields = frozen[evidence_id]["scope"]
            age_seconds = 0.0
        else:
            supplement = SUPPLEMENTAL_BY_ID[evidence_id]
            summary = supplement.summary
            scope_fields = {
                "user_id": supplement.user_id,
                "agent_id": supplement.agent_id,
                "session_id": supplement.session_id,
            }
            age_seconds = supplement.age_seconds
        scope = Scope(
            user_id=scope_fields["user_id"],
            agent_id=scope_fields["agent_id"],
            session_id=scope_fields.get("session_id") or "user",
        )
        row_id = await memory_repo.insert_episodic_memory(
            summary=summary, scope=scope, is_raw=0)
        if age_seconds > 0:
            await memory_repo._conn.execute(
                "UPDATE episodic_memories SET timestamp = timestamp - ? WHERE id = ?",
                (age_seconds, row_id))
            await memory_repo._conn.commit()
        await host.vector_store.index_memory(row_id, summary)
        stable_to_row[evidence_id] = row_id
    return stable_to_row


class PipelineRetrievalAdapter:
    """公开评测接缝适配器：retrieve_memories 直通完整 RetrievalEngine 管线。

    web.routers.retrieval 的 full 模式调用
    memory.retrieve_memories(query, k, scope, conv_user_id)——本适配器把该调用
    委托给真实 MemoryManager 入口，其余属性透传宿主，保证 channel/hybrid 等
    诊断模式同样走管线组件而非裸 SQL。
    """

    pipeline_name = "retrieval_engine.full"

    def __init__(self, host: RetrievalPipelineHost) -> None:
        self._host = host.memory_manager

    async def retrieve_memories(self, query: str, *, k: int, scope: Any,
                                conv_user_id: str = "",
                                apply_min_score: bool = True,
                                **_kwargs: Any) -> list[dict]:
        return await self._host.retrieve_memories(
            query, k=k, scope=scope, conv_user_id=conv_user_id,
            apply_min_score=apply_min_score)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._host, name)


def _result_ids(payload: dict) -> list[Any]:
    return [result["id"] for result in payload["results"]]


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


Gate = Callable[[dict, dict[str, int], dict], str | None]


def _gate_top1(evidence_id: str) -> Gate:
    def gate(payload: dict, rows: dict[str, int], case: dict) -> str | None:
        ids = _result_ids(payload)
        _require(ids, f"{case['id']}: 管线返回空结果")
        expected = rows[evidence_id]
        _require(
            ids[0] == expected,
            f"{case['id']}: 期望 {evidence_id}(row={expected}) 排第一，"
            f"实际顺序={ids}")
        return None
    return gate


def _gate_present(*evidence_ids: str) -> Gate:
    def gate(payload: dict, rows: dict[str, int], case: dict) -> str | None:
        ids = set(_result_ids(payload))
        missing = [eid for eid in evidence_ids if rows[eid] not in ids]
        _require(not missing,
                 f"{case['id']}: 证据未被召回 {missing}，实际顺序={_result_ids(payload)}")
        return None
    return gate


def _gate_absent(evidence_id: str) -> Gate:
    def gate(payload: dict, rows: dict[str, int], case: dict) -> str | None:
        leaked = [rid for rid in _result_ids(payload) if rid == rows[evidence_id]]
        _require(not leaked,
                 f"{case['id']}: 禁止出现的证据进入了结果 {evidence_id}"
                 f"(row={rows[evidence_id]})，实际顺序={_result_ids(payload)}")
        return None
    return gate


def _gate_ranked_before(first_id: str, second_id: str) -> Gate:
    def gate(payload: dict, rows: dict[str, int], case: dict) -> str | None:
        ids = _result_ids(payload)
        _require(rows[first_id] in ids and rows[second_id] in ids,
                 f"{case['id']}: 排序断言前两条证据都必须被召回")
        _require(ids.index(rows[first_id]) < ids.index(rows[second_id]),
                 f"{case['id']}: 期望 {first_id} 排在 {second_id} 之前"
                 f"（FSRS/时序回归信号），实际顺序={ids}")
        return f"order:{first_id}>{second_id}"
    return gate


def _gate_ndcg(expected: float) -> Gate:
    def gate(payload: dict, rows: dict[str, int], case: dict) -> str | None:
        actual = payload["metrics"]["ndcg"]
        # 浮点容差：完美排序的理论 1.0 经均值计算后可能带尾差
        _require(abs(actual - expected) < 1e-9,
                 f"{case['id']}: 期望 ndcg={expected}，实际 {actual}")
        return f"ndcg={actual}"
    return gate


def _gate_unanswerable(payload: dict, rows: dict[str, int],
                       case: dict) -> str | None:
    metrics = payload["metrics"]
    _require(metrics["ndcg"] == 0.0,
             f"{case['id']}: unanswerable 案例 ndcg 必须为 0")
    known_rows = set(rows.values())
    leaked_unknown = [rid for rid in _result_ids(payload) if rid not in known_rows]
    _require(not leaked_unknown,
             f"{case['id']}: 返回了夹具之外的行 {leaked_unknown}")
    false_positive = metrics["false_positive"]
    note = (
        f"false_positive={false_positive}"
        "（FTS 词法噪声属已知行为，过滤依赖 reranker/语义阈值，"
        "无 LLM 环境按观察记录不断言为零）")
    return note


CASE_BEHAVIOR_GATES: dict[str, list[Gate]] = {
    "exact-identifier-001": [_gate_top1("M:identity:1"), _gate_ndcg(1.0)],
    "coreference-001": [_gate_top1("M:plc-decision:1")],
    "multihop-001": [
        _gate_present("M:roommate:city", "M:roommate:job"),
        _gate_ndcg(1.0),
    ],
    "conflict-001": [
        _gate_present("M:coffee:v1", "M:coffee:v2"),
        _gate_ranked_before("M:coffee:v2", "M:coffee:v1"),
    ],
    "unanswerable-001": [_gate_unanswerable],
    "scope-isolation-001": [_gate_top1("M:bob:birthday"), _gate_ndcg(1.0)],
    "mixed-code-001": [_gate_top1("M:backend-stack:1")],
    "group-scope-001": [_gate_top1("KG:group-a:activity")],
    "exact-identifier-002": [_gate_top1("M:alice:phone"), _gate_ndcg(1.0)],
    "coreference-002": [_gate_top1("M:project-nightingale:1")],
    "multihop-002": [
        _gate_present("M:sister:cat-owner", "M:sister:cat-name"),
        _gate_ndcg(1.0),
    ],
    "conflict-002": [
        _gate_present("M:drink:v1", "M:drink:v2"),
        _gate_ranked_before("M:drink:v2", "M:drink:v1"),
    ],
    "scope-isolation-002": [
        _gate_top1("M:alice:passport"),
        _gate_absent("M:carol:passport"),
    ],
    "mixed-code-002": [_gate_top1("M:api-order-conflict:1")],
    "group-scope-002": [
        _gate_top1("KG:group-a:deadline"),
        _gate_absent("KG:group-b:deadline"),
    ],
}


async def assert_query_cache_roundtrip(host: RetrievalPipelineHost,
                                       query: str, scope: Any) -> str:
    """同一查询连打两次：第二次必须命中 query_cache 且结果一致。"""
    cache = host.memory_manager._query_cache
    first = await host.memory_manager.retrieve_memories(
        query, k=5, scope=scope, conv_user_id=scope.user_id)
    hits_before = cache.hits
    second = await host.memory_manager.retrieve_memories(
        query, k=5, scope=scope, conv_user_id=scope.user_id)
    _require(cache.hits > hits_before,
             "query_cache 第二次相同查询未命中（缓存管线回归）")
    _require([r["id"] for r in first] == [r["id"] for r in second],
             "query_cache 命中路径返回结果与首次检索不一致")
    return f"query_cache hits={cache.hits} misses={cache.misses}"
