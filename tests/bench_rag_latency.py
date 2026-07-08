"""RAG 管线延迟基准测试"""
import asyncio
import time
import sys
sys.path.insert(0, "/home/orangepi/ai-agent")

from memory.query_cache import QueryCache
from memory.retrieval_assessor import RetrievalAssessor
from memory.query_transform import QueryTransformer

async def main():
    qt = QueryTransformer()
    assessor = RetrievalAssessor()

    queries = [
        "昨天发生了什么",
        "你好啊",
        "Python和Java的区别",
        "如何配置数据库",
        "小妲之前帮我写了什么脚本",
    ]

    print("=== 1. 意图分类延迟 ===")
    for q in queries:
        t0 = time.perf_counter()
        intent = await qt.classify_intent(q)
        t1 = time.perf_counter()
        print(f"  {q[:20]:20s} -> {intent:10s}  {(t1-t0)*1000:.2f}ms")

    print("\n=== 2. HyDE 生成（无 API Key 降级）===")
    t0 = time.perf_counter()
    hyde = await qt.generate_hyde_document("如何配置数据库连接池")
    t1 = time.perf_counter()
    print(f"  result={hyde}  {(t1-t0)*1000:.2f}ms")

    print("\n=== 3. CRAG 评估器延迟 ===")
    mock = [{"rerank_score": 0.8}, {"rerank_score": 0.7}, {"rerank_score": 0.6}]
    t0 = time.perf_counter()
    for _ in range(1000):
        assessor.assess("test", mock)
    t1 = time.perf_counter()
    print(f"  1000次: {(t1-t0)*1000:.2f}ms  (avg {(t1-t0)/1000*1000:.4f}ms)")

    print("\n=== 4. QueryCache 延迟 ===")
    async def mock_embed(text):
        return [1.0, 0.0, 0.0]
    cache = QueryCache(embed_func=mock_embed, threshold=0.88)

    t0 = time.perf_counter()
    await cache.put("test", [{"id": 1}])
    t1 = time.perf_counter()
    print(f"  put: {(t1-t0)*1000:.2f}ms")

    t0 = time.perf_counter()
    r = await cache.get("test")
    t1 = time.perf_counter()
    print(f"  hit: {(t1-t0)*1000:.2f}ms  hit={r is not None}")

    t0 = time.perf_counter()
    r = await cache.get("different")
    t1 = time.perf_counter()
    print(f"  miss: {(t1-t0)*1000:.2f}ms")

    print("\n=== 5. 缓存加速（100次重复查询）===")
    async def simulate():
        await asyncio.sleep(0.001)
        return [{"id": 1}]

    t0 = time.perf_counter()
    for _ in range(100):
        await simulate()
    no_cache = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(100):
        cached = await cache.get("test")
        if cached is None:
            await cache.put("test", await simulate())
    with_cache = (time.perf_counter() - t0) * 1000

    print(f"  无缓存: {no_cache:.1f}ms")
    print(f"  有缓存: {with_cache:.1f}ms")
    print(f"  加速比: {no_cache/with_cache:.1f}x")

    print("\n=== 6. 新增组件总开销（单次查询）===")
    t0 = time.perf_counter()
    # 模拟一次完整新增开销：意图分类 + 评估
    await qt.classify_intent("如何配置数据库")
    assessor.assess("test", mock)
    t1 = time.perf_counter()
    print(f"  意图分类 + CRAG评估: {(t1-t0)*1000:.2f}ms")
    print("  （缓存命中时跳过全部检索，净延迟 < 0.1ms）")

asyncio.run(main())
