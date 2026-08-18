"""Tool Search v2 混合检索冒烟测试 + 量化评比.

测试维度:
  1. 向后兼容: 无 embed_client 时降级到纯 BM25 (v1 行为)
  2. BM25 中文分词: 单字粒度
  3. RRF 融合: 多路排序合并正确
  4. Vector 优雅降级: embed 失败不影响 BM25
  5. 量化评比: BM25 vs Hybrid 召回率对比

Run:
  python -m pytest tests/test_tool_search_hybrid.py -v
  python tests/test_tool_search_hybrid.py
"""
from __future__ import annotations

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))



def _build_test_tools():
    """构建测试工具集 (10 个工具, 覆盖中英文/同义词/近义概念)."""
    from tool_engine.tool_search import ToolDef
    return [
        ToolDef(
            name="web_search",
            description="Search the web for information. 搜索互联网获取信息.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            keywords=["search", "web", "internet", "搜索", "互联网"],
        ),
        ToolDef(
            name="web_browse",
            description="Browse a specific URL and extract page content. 浏览网页提取内容.",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}},
            keywords=["browse", "url", "page", "浏览", "网页"],
        ),
        ToolDef(
            name="weather_query",
            description="Get current weather for a location. 查询天气情况.",
            parameters={"type": "object", "properties": {"location": {"type": "string"}}},
            keywords=["weather", "temperature", "天气", "温度"],
        ),
        ToolDef(
            name="file_read",
            description="Read content from a local file. 读取本地文件内容.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            keywords=["file", "read", "文件", "读取"],
        ),
        ToolDef(
            name="file_write",
            description="Write content to a local file. 写入本地文件.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
            keywords=["file", "write", "文件", "写入"],
        ),
        ToolDef(
            name="mail_send",
            description="Send an email to recipients. 发送电子邮件.",
            parameters={"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}}},
            keywords=["mail", "email", "send", "邮件", "发送"],
        ),
        ToolDef(
            name="code_execute",
            description="Execute Python code and return output. 执行 Python 代码.",
            parameters={"type": "object", "properties": {"code": {"type": "string"}}},
            keywords=["code", "python", "execute", "代码", "执行"],
        ),
        ToolDef(
            name="image_generate",
            description="Generate an image from text description. 文生图 AI 生成图片.",
            parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
            keywords=["image", "generate", "picture", "图片", "生成"],
        ),
        ToolDef(
            name="reminder_set",
            description="Set a reminder for a specific time. 设置提醒事项.",
            parameters={"type": "object", "properties": {"time": {"type": "string"}, "content": {"type": "string"}}},
            keywords=["reminder", "alarm", "提醒", "闹钟"],
        ),
        ToolDef(
            name="translate_text",
            description="Translate text between languages. 翻译文本到不同语言.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}, "target_lang": {"type": "string"}}},
            keywords=["translate", "translation", "翻译", "语言"],
        ),
    ]


# ── 1. 向后兼容: 纯 BM25 (无 embed_client) ──────────────────────

class TestBackwardCompat:
    """v1 行为: 无 embed_client 时降级到纯 BM25."""

    def test_pure_bm25_no_vector(self):
        """无 embed_client 时, search 不崩溃且返回结果."""
        from tool_engine.tool_search import ToolSearchEngine
        engine = ToolSearchEngine()
        for tool in _build_test_tools():
            engine.register(tool)

        results = engine.search("搜索互联网", top_k=3)
        assert len(results) > 0
        # web_search 应该排前 3 (BM25 命中 "搜索" "互联网")
        names = [t.name for t in results]
        assert "web_search" in names, f"web_search 应在结果中, 实际 {names}"

    def test_stats_no_vector(self):
        """stats.vector_enabled 为 False (无 embed_client)."""
        from tool_engine.tool_search import ToolSearchEngine
        engine = ToolSearchEngine()
        engine.register(_build_test_tools()[0])
        stats = engine.get_stats()
        assert stats["vector_enabled"] is False
        assert stats["hybrid_search_count"] == 0


# ── 2. BM25 中文分词 ──────────────────────────────────────────

class TestBM25Chinese:
    """BM25 中文分词 (单字粒度)."""

    def test_chinese_keyword_match(self):
        """中文关键词应能命中."""
        from tool_engine.tool_search import BM25Index
        idx = BM25Index()
        for tool in _build_test_tools():
            idx.add_tool(tool)

        # "天气" 应命中 weather_query
        results = idx.search("天气", top_k=3)
        names = [t.name for t, _ in results]
        assert "weather_query" in names, f"天气应命中 weather_query, 实际 {names}"

    def test_english_keyword_match(self):
        """英文关键词应能命中."""
        from tool_engine.tool_search import BM25Index
        idx = BM25Index()
        for tool in _build_test_tools():
            idx.add_tool(tool)

        results = idx.search("weather", top_k=3)
        names = [t.name for t, _ in results]
        assert "weather_query" in names


# ── 3. RRF 融合 ──────────────────────────────────────────────

class TestRRF:
    """Reciprocal Rank Fusion."""

    def test_rrf_basic(self):
        """RRF 基本融合."""
        from tool_engine.tool_search import reciprocal_rank_fusion
        # BM25 排序: [a, b, c]
        # Vector 排序: [b, c, d]
        # RRF 融合: b 和 c 在两路都靠前, 应排前
        bm25 = ["a", "b", "c"]
        vector = ["b", "c", "d"]
        fused = reciprocal_rank_fusion([bm25, vector], k=60, limit=4)
        names = [name for name, _ in fused]
        # b 在 BM25 第 2, Vector 第 1 → 分数最高
        assert names[0] == "b", f"b 应排第一, 实际 {names}"
        # c 在 BM25 第 3, Vector 第 2 → 第二
        assert names[1] == "c", f"c 应排第二, 实际 {names}"

    def test_rrf_single_list(self):
        """单路 RRF 应等于原排序."""
        from tool_engine.tool_search import reciprocal_rank_fusion
        single = ["a", "b", "c"]
        fused = reciprocal_rank_fusion([single], k=60, limit=3)
        assert [name for name, _ in fused] == single

    def test_rrf_empty(self):
        """空列表 RRF."""
        from tool_engine.tool_search import reciprocal_rank_fusion
        assert reciprocal_rank_fusion([], limit=5) == []
        assert reciprocal_rank_fusion([[]], limit=5) == []


# ── 4. Vector 优雅降级 ───────────────────────────────────────

class TestVectorGracefulDegradation:
    """Vector 检索优雅降级."""

    def test_vector_no_client(self):
        """无 embed_client 时 VectorIndex.search 返回空."""
        from tool_engine.tool_search import VectorIndex
        idx = VectorIndex(embed_client=None)
        assert idx._enabled is False
        for tool in _build_test_tools():
            idx.add_tool(tool)
        results = idx.search("query", top_k=5)
        assert results == []

    def test_vector_mock_client_failure(self):
        """mock client 抛异常时, search 返回空 (BM25 兜底)."""
        from tool_engine.tool_search import VectorIndex

        class FailingClient:
            class embeddings:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("API unavailable")

        idx = VectorIndex(embed_client=FailingClient())
        assert idx._enabled is True
        for tool in _build_test_tools():
            idx.add_tool(tool)
        # embed 失败, search 应返回空
        results = idx.search("weather", top_k=5)
        assert results == [], "embed 失败时应返回空, 让 BM25 兜底"


# ── 5. 量化评比: BM25 vs Hybrid 召回率 ───────────────────────

class TestQuantitativeBenchmark:
    """量化评比: BM25 vs Hybrid 召回率对比.

    场景: 用户用同义词/近义概念查询时, BM25 可能漏召回,
          向量检索应补足语义相似但词法不匹配的工具.
    """

    def _setup_bm25_engine(self):
        """纯 BM25 引擎."""
        from tool_engine.tool_search import ToolSearchEngine
        engine = ToolSearchEngine()
        for tool in _build_test_tools():
            engine.register(tool)
        return engine

    def _setup_hybrid_engine(self, mock_vectors: dict[str, list[float]]):
        """混合检索引擎 (mock 向量)."""
        from tool_engine.tool_search import ToolSearchEngine, VectorIndex

        # 覆盖 VectorIndex 的 _embed_sync, 返回预设向量
        class MockVectorIndex(VectorIndex):
            def __init__(self):
                super().__init__(embed_client=object())  # 占位 client
                self._mock_vectors = mock_vectors

            def _embed_sync(self, text):
                return self._mock_vectors.get(text, [])

        engine = ToolSearchEngine()
        for tool in _build_test_tools():
            engine.register(tool)
        # 替换为 mock vector index
        engine._vector_index = MockVectorIndex()
        for tool in engine._index._tool_defs:
            engine._vector_index.add_tool(tool)
        return engine

    def test_recall_comparison(self):
        """同义词查询: BM25 vs Hybrid 召回率对比.

        查询 "如何发邮件" (含"发"+"邮件"):
          - BM25: 命中 mail_send (关键词 "邮件" "发送")
          - Hybrid: BM25 + Vector (向量也命中 mail_send)
        """
        bm25_engine = self._setup_bm25_engine()

        # mock 向量: 让 "如何发邮件" 和 mail_send 的描述语义相似
        # 用简单向量: [1, 0] = 邮件类, [0, 1] = 非邮件类
        mock_vectors = {
            "web_search Search the web for information. 搜索互联网获取信息. search web internet 搜索 互联网": [0.1, 0.9],
            "web_browse Browse a specific URL and extract page content. 浏览网页提取内容. browse url page 浏览 网页": [0.2, 0.8],
            "weather_query Get current weather for a location. 查询天气情况. weather temperature 天气 温度": [0.1, 0.8],
            "file_read Read content from a local file. 读取本地文件内容. file read 文件 读取": [0.1, 0.8],
            "file_write Write content to a local file. 写入本地文件. file write 文件 写入": [0.1, 0.8],
            "mail_send Send an email to recipients. 发送电子邮件. mail email send 邮件 发送": [0.9, 0.1],
            "code_execute Execute Python code and return output. 执行 Python 代码. code python execute 代码 执行": [0.1, 0.8],
            "image_generate Generate an image from text description. 文生图 AI 生成图片. image generate picture 图片 生成": [0.1, 0.8],
            "reminder_set Set a reminder for a specific time. 设置提醒事项. reminder alarm 提醒 闹钟": [0.1, 0.8],
            "translate_text Translate text between languages. 翻译文本到不同语言. translate translation 翻译 语言": [0.1, 0.8],
            "如何发邮件": [0.95, 0.05],  # 语义接近 mail_send
        }
        hybrid_engine = self._setup_hybrid_engine(mock_vectors)

        # 查询 "如何发邮件"
        query = "如何发邮件"
        bm25_results = bm25_engine.search(query, top_k=3)
        hybrid_results = hybrid_engine.search(query, top_k=3)

        bm25_names = {t.name for t in bm25_results}
        hybrid_names = {t.name for t in hybrid_results}

        # 两者都应包含 mail_send
        assert "mail_send" in bm25_names, f"BM25 应命中 mail_send, 实际 {bm25_names}"
        assert "mail_send" in hybrid_names, f"Hybrid 应命中 mail_send, 实际 {hybrid_names}"

        # Hybrid 中 mail_send 应排第一 (BM25 + Vector 双路命中)
        assert hybrid_results[0].name == "mail_send", \
            f"Hybrid mail_send 应排第一, 实际第一是 {hybrid_results[0].name}"

    def test_semantic_recall_boost(self):
        """语义召回提升: 同义概念查询, BM25 漏召回时 Vector 补足.

        查询 "寄信" (BM25 可能漏, 因为 "寄信" 不在 keywords 中):
          - BM25: 不命中 mail_send (因为 "寄" "信" 不在 mail_send 的 keywords)
          - Vector: 命中 mail_send (语义相近)
        """
        bm25_engine = self._setup_bm25_engine()

        # mock 向量: "寄信" 语义接近 mail_send
        mock_vectors = {
            "mail_send Send an email to recipients. 发送电子邮件. mail email send 邮件 发送": [0.9, 0.1],
            "寄信": [0.85, 0.15],  # 语义接近 mail_send
            # 其他工具给随机向量
            "web_search Search the web for information. 搜索互联网获取信息. search web internet 搜索 互联网": [0.1, 0.9],
            "web_browse Browse a specific URL and extract page content. 浏览网页提取内容. browse url page 浏览 网页": [0.2, 0.8],
            "weather_query Get current weather for a location. 查询天气情况. weather temperature 天气 温度": [0.1, 0.8],
            "file_read Read content from a local file. 读取本地文件内容. file read 文件 读取": [0.1, 0.8],
            "file_write Write content to a local file. 写入本地文件. file write 文件 写入": [0.1, 0.8],
            "code_execute Execute Python code and return output. 执行 Python 代码. code python execute 代码 执行": [0.1, 0.8],
            "image_generate Generate an image from text description. 文生图 AI 生成图片. image generate picture 图片 生成": [0.1, 0.8],
            "reminder_set Set a reminder for a specific time. 设置提醒事项. reminder alarm 提醒 闹钟": [0.1, 0.8],
            "translate_text Translate text between languages. 翻译文本到不同语言. translate translation 翻译 语言": [0.1, 0.8],
        }
        hybrid_engine = self._setup_hybrid_engine(mock_vectors)

        query = "寄信"
        bm25_results = bm25_engine.search(query, top_k=3)
        hybrid_results = hybrid_engine.search(query, top_k=3)

        bm25_names = {t.name for t in bm25_results}
        hybrid_names = {t.name for t in hybrid_results}

        # BM25 可能漏召回 mail_send (因为 "寄信" 不在 mail_send 的 keywords)
        bm25_hit = "mail_send" in bm25_names
        # Hybrid 应命中 mail_send (Vector 补足)
        hybrid_hit = "mail_send" in hybrid_names

        print(f"\n  查询 '{query}':")
        print(f"    BM25 结果: {bm25_names}")
        print(f"    Hybrid 结果: {hybrid_names}")
        print(f"    BM25 命中 mail_send: {bm25_hit}")
        print(f"    Hybrid 命中 mail_send: {hybrid_hit}")

        # Hybrid 必须命中 (Vector 补足语义)
        assert hybrid_hit, f"Hybrid 应通过向量检索命中 mail_send, 实际 {hybrid_names}"
        # 如果 BM25 漏召回, Hybrid 应补足 → 召回率提升
        if not bm25_hit:
            print("  ✓ 语义召回提升: BM25 漏召回, Hybrid 通过向量补足")


# ── 主入口 ───────────────────────────────────────────────────

async def main():
    """运行所有测试."""
    print("\n" + "=" * 60)
    print("  Tool Search v2 混合检索冒烟测试 + 量化评比")
    print("=" * 60)

    # 1. 向后兼容
    print("\n--- 1. 向后兼容 (无 embed_client) ---")
    compat = TestBackwardCompat()
    compat.test_pure_bm25_no_vector()
    print("  test_pure_bm25_no_vector ✓")
    compat.test_stats_no_vector()
    print("  test_stats_no_vector ✓")

    # 2. BM25 中文
    print("\n--- 2. BM25 中文分词 ---")
    zh = TestBM25Chinese()
    zh.test_chinese_keyword_match()
    print("  test_chinese_keyword_match ✓")
    zh.test_english_keyword_match()
    print("  test_english_keyword_match ✓")

    # 3. RRF 融合
    print("\n--- 3. RRF 融合 ---")
    rrf = TestRRF()
    rrf.test_rrf_basic()
    print("  test_rrf_basic ✓")
    rrf.test_rrf_single_list()
    print("  test_rrf_single_list ✓")
    rrf.test_rrf_empty()
    print("  test_rrf_empty ✓")

    # 4. Vector 优雅降级
    print("\n--- 4. Vector 优雅降级 ---")
    deg = TestVectorGracefulDegradation()
    deg.test_vector_no_client()
    print("  test_vector_no_client ✓")
    deg.test_vector_mock_client_failure()
    print("  test_vector_mock_client_failure ✓")

    # 5. 量化评比
    print("\n--- 5. 量化评比: BM25 vs Hybrid ---")
    bench = TestQuantitativeBenchmark()
    bench.test_recall_comparison()
    print("  test_recall_comparison ✓")
    bench.test_semantic_recall_boost()
    print("  test_semantic_recall_boost ✓")

    print("\n" + "=" * 60)
    print("  所有测试通过 ✓")
    print("=" * 60)
    print("  量化结论:")
    print("    - 向后兼容: 无 embed_client 时降级到纯 BM25 (v1 行为)")
    print("    - 优雅降级: embed 失败不影响 BM25 检索")
    print("    - 语义召回: 同义词/近义概念查询, Hybrid 通过向量补足")
    print("    - RRF 融合: 双路命中项排前, 单路命中项排后")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
