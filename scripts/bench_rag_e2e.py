#!/usr/bin/env python3
"""真实端到端 RAG 接管线基准测试

用真实 DB (data/agent.db + agent_vec.db) + 真实检索链路，不 mock LLM。
- 正向：query → retrieve_memories → 结果（耗时/心跳/命中）
- 反向：结果质量分析（score 分布 / 来源 / 相关性）

用法: .venv/bin/python scripts/bench_rag_e2e.py
"""
import asyncio
import os
import sys
import time
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

# 加载 .env（与生产一致的真实 API key / 配置）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    logger.warning("bench_rag.env_load_failed", exc_info=True)

# 保留 INFO 级 retrieve_stage 日志，用于定位阻塞阶段
try:
    from loguru import logger as _lg
    _lg.remove()
    _lg.add(sys.stderr, level="INFO", format="<level>{level}</level> {message}")
except Exception:
    logger.warning("bench_rag.loguru_setup_failed", exc_info=True)


async def heartbeat_monitor(stop_event):
    """事件循环心跳监测：每 5ms 醒一次，测量实际延迟"""
    latencies = []
    while not stop_event.is_set():
        start = time.monotonic()
        await asyncio.sleep(0.005)
        elapsed = time.monotonic() - start
        latencies.append(max(0.0, (elapsed - 0.005) * 1000))
    return latencies


async def bench_retrieve(memory, query, k=5):
    """单次检索：测耗时 + 心跳 + 结果"""
    stop_event = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat_monitor(stop_event))
    t0 = time.monotonic()
    results = await memory.retrieve_memories(query, k=k)
    elapsed = (time.monotonic() - t0) * 1000
    stop_event.set()
    hb = await hb_task
    max_hb = max(hb) if hb else 0.0
    return results, elapsed, max_hb


def fmt_verdict(max_hb):
    if max_hb > 100:
        return "❌严重阻塞"
    if max_hb > 50:
        return "⚠️阻塞"
    if max_hb > 20:
        return "⚠️轻微"
    return "✓流畅"


async def main():
    from agent_core.core import AgentCore

    print("=" * 70)
    print("  真实端到端 RAG 接管线基准（data/agent.db + agent_vec.db）")
    print("  心跳 >50ms = 阻塞；>100ms = 严重阻塞")
    print("=" * 70)

    # ── 初始化真实 AgentCore ──
    print("\n[初始化] AgentCore.init() ...")
    t0 = time.monotonic()
    core = AgentCore()
    await core.init()
    init_ms = (time.monotonic() - t0) * 1000
    print(f"  初始化完成: {init_ms:.0f}ms")

    if not core.memory:
        print("ERROR: MemoryManager 未初始化，退出")
        return

    # ── 正向：多个真实 query 检索 ──
    # 覆盖：时间型 / 偏好型 / 技术型 / 闲聊型 / 回忆型
    queries = [
        "昨天发生了什么",
        "用户喜欢什么编程语言",
        "Python 怎么配置数据库连接",
        "小妲之前帮我写过什么脚本",
        "你好啊",
        "本能管理器怎么工作的",
        "用户被称呼为什么",
        "Windows 安装包怎么构建",
    ]

    print("\n" + "─" * 70)
    print("  正向：query → retrieve_memories → 结果")
    print("─" * 70)

    # 预热检索（不计时）：触发首次 lazy import / 模型加载 / API 连接
    # 用于验证"首次阻塞是否来自首次加载"
    print("\n[预热] 触发首次加载（不计时）...")
    t_warm = time.monotonic()
    await core.memory.retrieve_memories("预热查询触发首次加载", k=1)
    print(f"  预热耗时: {(time.monotonic() - t_warm)*1000:.0f}ms")

    all_durations = []
    all_hb = []
    for q in queries:
        results, elapsed, max_hb = await bench_retrieve(core.memory, q, k=5)
        all_durations.append(elapsed)
        all_hb.append(max_hb)
        verdict = fmt_verdict(max_hb)
        print(f"\n[Q] {q}")
        print(f"  耗时 {elapsed:7.1f}ms | 结果 {len(results)} 条 | 心跳max {max_hb:6.1f}ms {verdict}")
        # 反向：结果质量分析
        if results:
            scores = []
            sources = {}
            for r in results:
                score = (r.get("rerank_score") or r.get("score")
                         or r.get("rrf_score") or 0.0)
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 0.0
                scores.append(score)
                src = r.get("source", "unknown")
                sources[src] = sources.get(src, 0) + 1
            if scores:
                print(f"  score: max={max(scores):.3f} min={min(scores):.3f} "
                      f"avg={statistics.mean(scores):.3f}")
            print(f"  来源分布: {sources}")
            # 展示 top 2 结果摘要
            for i, r in enumerate(results[:2]):
                summary = (r.get("summary") or r.get("content") or "")[:70]
                print(f"    #{i+1} | {summary}")
        else:
            print("  (无结果)")

    # ── 汇总 ──
    print("\n" + "─" * 70)
    print("  汇总")
    print("─" * 70)
    print(f"  检索耗时: p50={statistics.median(all_durations):.1f}ms "
          f"max={max(all_durations):.1f}ms")
    print(f"  心跳: max={max(all_hb):.1f}ms (标准: <20ms 流畅)")
    blocking = sum(1 for h in all_hb if h > 50)
    print(f"  阻塞查询数: {blocking}/{len(all_hb)}")

    # ── 缓存命中测试（同一 query 第二次）──
    print("\n" + "─" * 70)
    print("  缓存命中测试（同一 query 重复检索）")
    print("─" * 70)
    q = "用户喜欢什么编程语言"
    _, t1, _ = await bench_retrieve(core.memory, q, k=5)
    _, t2, hb2 = await bench_retrieve(core.memory, q, k=5)
    _, t3, hb3 = await bench_retrieve(core.memory, q, k=5)
    speedup = t1 / max(t2, 0.1)
    print(f"  首次:   {t1:7.1f}ms")
    print(f"  第2次:  {t2:7.1f}ms (缓存) 心跳 {hb2:.1f}ms")
    print(f"  第3次:  {t3:7.1f}ms (缓存) 心跳 {hb3:.1f}ms")
    print(f"  加速比: {speedup:.1f}x")

    # ── 高频压力：同一 query 连续 20 次 ──
    print("\n" + "─" * 70)
    print("  高频压力（同一 query 连续 20 次，测缓存稳定性）")
    print("─" * 70)
    durations = []
    hb_list = []
    for _ in range(20):
        _, d, h = await bench_retrieve(core.memory, q, k=5)
        durations.append(d)
        hb_list.append(h)
    durations.sort()
    print(f"  耗时: p50={statistics.median(durations):.1f}ms "
          f"p99={durations[int(len(durations)*0.99)]:.1f}ms "
          f"max={max(durations):.1f}ms")
    print(f"  心跳: max={max(hb_list):.1f}ms {fmt_verdict(max(hb_list))}")

    print("\n" + "=" * 70)
    print("  评判标准：心跳 max < 20ms 流畅 | 20-50ms 轻微 | >50ms 阻塞")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
