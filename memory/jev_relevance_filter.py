"""Jev 记忆相关性过滤 —— 检索召回后的语义后置过滤层。

定位（Jev 能力探索报告 · 2026-09-23 实测，P1 推荐项）：
    现有过滤依赖**数值分数**（交叉编码器 rerank_score / RRF 分 / final_score）。
    数值分数的问题（项目内已有铁证，见 pipeline.py:1068 注释）：
        "技术型 query 返回 rerank 0.007 的亲密内容" —— 数值低但仍在候选里；
        反之也有"分数尚可但语义跑题"的情况，数值阈值无法区分。
    Jev 的 Score 原语做**语义相关性**判断（「这条记忆对回答该问题有多大用」），
    与数值分数互补：数值看词的相似度，Jev 看**意图的契合度**。

实测依据（报告 E1 + 守则）：
    报告实测 E1「记忆批量过滤」：`2.27 · 置信 0.71`，判定为"合理"。
    报告同时给出两条关键约束：
      1. **每条记忆必须作为独立小请求，或与 query 一起带齐 state 再评**——
         实测 A2 坏实验的教训：问题引用的依据若不在 state 里，confidence
         会崩到 0.04、概率两极分化（"state 缺依据，输出就退化"）。
      2. **一次请求可并行问多个问题**，加题几乎不增时延（官方实测 13 题
         批量比逐个快 ~10 倍）——所以优先**批量合并成一次调用**。

设计：批量 Score，一次请求评完所有候选
    把 query 与全部候选记忆一起放进 state，用**多个 Score 问题**（每题对应
    一条候选）一次性评完，而不是对每条候选发一次请求。
    Score 档位：0=无关 / 1=略有关系 / 2=相关但不直接 / 3=直接相关。

安全约束（与其它 Jev 接入点一致）：
    - 未启用/未配置/超时/异常 → 返回 None，调用方保留原有数值过滤（fail-soft）。
    - 本层**只做丢弃，不做注入**：绝不会把原本被过滤掉的记忆捞回来。
    - 每轮至多过滤 ``MAX_DROP_RATIO`` 比例的候选，防止模型误判导致上下文枯竭。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

# Score 档位（有序，下标即分值）——措辞要具体到"两个审查者能达成一致"
_SCORE_LEVELS = [
    "无关：与问题主题没有任何关联，或纯属其他话题",
    "略有关系：领域相近或提及相同名词，但不解答问题",
    "相关：提供背景或间接线索，对回答有参考价值",
    "直接相关：直接包含回答该问题所需的信息",
]

# 单次请求最多评多少条候选（防御性上限，避免 state 过长稀释准确率）
MAX_CANDIDATES = 20

# 每轮最多过滤掉的比例（防止误判导致上下文枯竭）
MAX_DROP_RATIO = 0.5

# 低于此分视为「可丢弃」（0=无关；1=略有关系仍保留）
DEFAULT_DROP_BELOW = 1.0


def _config_float(name: str, default: float) -> float:
    try:
        import config as _cfg
        return float(getattr(_cfg, name, default))
    except (ImportError, AttributeError, ValueError, TypeError):
        return default


def is_enabled() -> bool:
    """过滤层是否启用（JEV_ENABLED 且已配置密钥）。"""
    try:
        from utils import jev_client as jev
    except ImportError:
        return False
    return jev.is_available()


def _memory_text(item: dict) -> str:
    """取记忆正文（项目内真正进 prompt 的字段是 summary，不是 content）。"""
    return str(item.get("summary") or item.get("content") or "").strip()


async def filter_memories(query: str, results: list[dict], *,
                          drop_below: float | None = None) -> list[dict] | None:
    """用 Jev 语义相关性过滤检索结果。

    Returns:
        - 过滤后的列表（可能比输入短，也可能原样返回）
        - ``None`` 表示未启用/失败 —— 调用方保留原有数值过滤（fail-soft）

    只丢弃、不注入：输出必然是输入的子集（保序）。
    """
    if not results:
        return None
    if not is_enabled():
        return None

    try:
        from utils import jev_client as jev
    except ImportError:
        return None

    if drop_below is None:
        drop_below = _config_float("JEV_RELEVANCE_DROP_BELOW", DEFAULT_DROP_BELOW)

    # 候选截断（state 过长会稀释准确率——报告守则 5「context rot」）
    candidates = results[:MAX_CANDIDATES]

    # 一次请求评完所有候选：每条候选一个 Score 问题（批量，加题几乎不增时延）。
    # ⚠️ 实测教训：问题引用的依据必须全部在 state 里（否则 confidence 崩）。
    # 因此把 query 与每条候选文本一起结构化放进 state，问题里用反引号引用字段路径。
    state: dict[str, Any] = {"query": query, "memories": {}}
    questions: dict[str, dict] = {}
    key_to_index: dict[str, int] = {}
    for idx, item in enumerate(candidates):
        text = _memory_text(item)
        if not text:
            continue
        key = f"m{idx}"
        state["memories"][key] = text[:600]
        key_to_index[key] = idx
        questions[key] = jev.score(
            instructions=(
                f"记忆 `memories.{key}` 对回答 `query` 有多大的参考价值？"
                "判断依据是它能否提供回答所需的信息，而非话题是否沾边。"
            ),
            levels=_SCORE_LEVELS,
        )

    if not questions:
        return None

    timeout = _config_float("JEV_TIMEOUT", 8.0)
    try:
        answers = await jev.system_one(state=state, questions=questions, timeout=timeout)
    except ValueError:
        return None
    except Exception:
        logger.exception("jev_relevance_filter.unexpected_error")
        return None

    if not answers:
        return None

    # 逐条读取分数；取不到分数的候选一律**保留**（fail-soft，不因缺失而误删）
    kept: list[dict] = []
    dropped: list[dict] = []
    for idx, item in enumerate(candidates):
        key = f"m{idx}"
        if key not in key_to_index:
            # 无正文的候选：保留（不参与评分，也不因评分缺失被删）
            kept.append(item)
            continue
        score = jev.answer_score(answers, key)
        if score is None:
            kept.append(item)
            continue
        if score < drop_below:
            dropped.append((score, item))
        else:
            kept.append(item)

    # 兜底：过滤比例上限（防模型误判把上下文清空）
    max_drop = int(len(candidates) * MAX_DROP_RATIO)
    if len(dropped) > max_drop:
        dropped.sort(key=lambda pair: pair[0])  # 分数最低的优先丢，保住其余
        recovered = dropped[max_drop:]
        dropped = dropped[:max_drop]
        kept.extend(item for _score, item in recovered)
        logger.warning("jev_relevance_filter.drop_capped",
                       cap=max_drop, requested=len(dropped) + len(recovered),
                       recovered=len(recovered))

    # 超出 MAX_CANDIDATES 的尾部候选原样保留（未参与评分）
    kept.extend(results[MAX_CANDIDATES:])

    if not dropped:
        return results

    # 保序返回（按原输入顺序）
    dropped_ids = {id(item) for _s, item in dropped}
    ordered = [r for r in results if id(r) not in dropped_ids]
    logger.info("jev_relevance_filter.applied",
                query=query[:60], before=len(results), after=len(ordered),
                dropped=[round(s, 2) for s, _ in dropped][:8])
    return ordered


async def apply_and_mark(query: str, results: list[dict],
                         mark_dropped: Any = None,
                         *, apply_min_score: bool = True,
                         intent: str = "factual") -> list[dict]:
    """一站式入口：门控 + 过滤 + 打点，供检索流水线一行调用。

    抽出此函数是为了让 pipeline 不因过滤逻辑而膨胀（巨型文件止血液轮）：
    调用方只需 ``results = await apply_and_mark(query, results, mark, ...)``，
    连"是否需要过滤"的判断也收敛在此，避免调用点散落条件分支。

    Args:
        query: 用户查询。
        results: 候选记忆列表。
        mark_dropped: 可选回调 ``(memory_id, reason)``，用于标记被丢弃项（打点/审计）。
        apply_min_score: 全局过滤开关（与原有数值过滤同源）。
        intent: 检索意图；闲聊型不做过滤（与原有数值过滤语义一致）。

    Returns:
        过滤后的列表；不满足过滤条件/未启用/失败时**原样返回输入**，
        让调用方无需区分多种跳过形态。
    """
    if not apply_min_score or intent == "chat" or not results:
        return results
    filtered = await filter_memories(query, results)
    if filtered is None or len(filtered) >= len(results):
        return results
    if mark_dropped is not None:
        kept_ids = {id(item) for item in filtered}
        for item in results:
            if id(item) not in kept_ids:
                try:
                    mark_dropped(item.get("id"), "jev_low_relevance")
                except Exception:
                    logger.debug("jev_relevance_filter.mark_failed", exc_info=True)
    return filtered


async def post_filter(query: str, results: list[dict], config: Any,
                       intent: str, apply_min_score: bool,
                       passes_min_relevance: Any = None,
                       mark_dropped: Any = None) -> list[dict]:
    """检索后置过滤总入口：Jev 语义过滤 + 原有数值最低分过滤。

    抽出此函数让 pipeline 的调用点收敛为一行（巨型文件止血液轮）。
    两层顺序固定：先 Jev 语义（意图契合度），再数值（rerank 相似度）——
    数值层依赖 rerank_score，而 Jev 层可能已剔除部分候选，两者互补不冲突。

    Args:
        query: 用户查询。
        results: 候选记忆。
        config: 配置对象（读 RAG_MIN_FINAL_SCORE）。
        intent: 检索意图；闲聊型跳过所有过滤。
        apply_min_score: 全局过滤开关。
        passes_min_relevance: 数值最低分判定回调 ``(result, min_score) -> bool``；
            None 表示跳过数值层（仅做 Jev 层）。
        mark_dropped: 丢弃打点回调 ``(memory_id, reason)``。

    Returns:
        过滤后的列表（输出必为输入子集，保序）。
    """
    if not apply_min_score or intent == "chat" or not results:
        return results

    # 第一层：Jev 语义相关性（未启用/失败时原样返回，fail-soft）
    results = await apply_and_mark(query, results, mark_dropped,
                                   apply_min_score=True, intent=intent)
    if not results:
        return results

    # 第二层：原有数值最低分过滤（保留话题触发记忆）
    _min_score = getattr(config, "RAG_MIN_FINAL_SCORE", 0.15)
    if passes_min_relevance is None or not (_min_score > 0):
        return results
    _before = len(results)
    before_results = list(results)
    results = [r for r in results if passes_min_relevance(r, _min_score)]
    if mark_dropped is not None:
        kept = {id(item) for item in results}
        for dropped in before_results:
            if id(dropped) not in kept:
                mark_dropped(dropped.get("id"), "low_score")
    if len(results) != _before:
        logger.info("memory.low_score_filtered", query=query[:60],
                    before=_before, after=len(results), min_score=_min_score)
    return results


async def health_check() -> dict:
    """健康检查（供 /system 健康汇总）。"""
    return {
        "available": is_enabled(),
        "drop_below": _config_float("JEV_RELEVANCE_DROP_BELOW", DEFAULT_DROP_BELOW),
        "max_candidates": MAX_CANDIDATES,
        "max_drop_ratio": MAX_DROP_RATIO,
    }


__all__ = [
    "DEFAULT_DROP_BELOW",
    "MAX_CANDIDATES",
    "MAX_DROP_RATIO",
    "apply_and_mark",
    "filter_memories",
    "health_check",
    "is_enabled",
    "post_filter",
]
