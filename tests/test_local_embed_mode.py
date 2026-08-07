"""本地 Embedding（EMBED_MODE=local，BGE-small-zh-v1.5）集成测试。

覆盖：
1. Provider：512 维 / L2 归一化 / 语义相近文本相似度更高
2. VectorStore local 模式：新库初始化 512 维表、upsert + search 检索质量
3. 维度不匹配防护：1024 维旧库在 local 模式 init 时报 RuntimeError（提示重建）

依赖真实模型目录（默认 /media/orangepi/KIOXIA/nahida-data/models/bge-small-zh-v1.5），
模型缺失/依赖缺失时跳过（不视为失败）。
"""
import asyncio
import math
import sqlite3
import tempfile
from pathlib import Path

import pytest

from memory.local_embed import HAS_LOCAL_EMBED_DEPS, LocalEmbeddingProvider
from memory.vector_store import HAS_SQLITE_VEC, VectorStore

MODEL_DIR = Path("/media/orangepi/KIOXIA/nahida-data/models/bge-small-zh-v1.5")

pytestmark = pytest.mark.skipif(
    not (HAS_LOCAL_EMBED_DEPS and HAS_SQLITE_VEC and MODEL_DIR.exists()
         and ((MODEL_DIR / "model.onnx").exists() or (MODEL_DIR / "onnx/model.onnx").exists())),
    reason="本地模型或依赖缺失，跳过 local embed 集成测试",
)


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


def test_provider_512dim_normalized_semantic():
    p = LocalEmbeddingProvider(MODEL_DIR, query_prefix="")
    assert p.load(), f"provider load failed: {p._load_error}"
    assert p.dimensions == 512

    v1 = p.embed("RAG是什么")
    v2 = p.embed("检索增强生成是通过外部知识提升大模型回答准确性的技术")
    v3 = p.embed("今天天气很好，适合出去散步")

    assert len(v1) == 512 and len(v2) == 512 and len(v3) == 512
    norm = math.sqrt(sum(x * x for x in v1))
    assert abs(norm - 1.0) < 1e-4, "向量未 L2 归一化"
    # 语义相近 > 语义无关
    assert _cos(v1, v2) > _cos(v1, v3)
    assert _cos(v1, v2) > 0.3, "RAG 相关文本相似度过低"


def test_vector_store_local_init_upsert_search():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            vec_db = Path(td) / "vec.db"
            vs = VectorStore(
                db_path=str(vec_db),
                embed_mode="local",
                local_model_dir=str(MODEL_DIR),
            )
            await vs.init()
            assert vs.ready
            assert vs.dimensions == 512

            ok1 = await vs.upsert(1, "RAG是什么")
            ok2 = await vs.upsert(2, "检索增强生成通过外部知识提升回答准确性")
            ok3 = await vs.upsert(3, "纳西妲是须弥的草神")
            assert ok1 and ok2 and ok3

            hits = await vs.search("什么是RAG", top_k=3)
            assert hits, "local 模式检索无结果"
            top_id = hits[0][0]
            assert top_id in (1, 2), f"语义检索应命中 RAG 相关记录，实际 top={top_id}"
            await vs.close()

    asyncio.run(run())


def test_local_mode_dimension_mismatch_guard():
    async def run():
        with tempfile.TemporaryDirectory() as td:
            vec_db = Path(td) / "vec.db"
            # 预建 1024 维旧库（模拟 BGE-M3 时代）
            import sqlite_vec

            conn = sqlite3.connect(str(vec_db))
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute("CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[1024])")
            vec_1024 = "[" + ",".join(["0.1"] * 1024) + "]"
            conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (1, vec_f32(?))",
                (vec_1024,),
            )
            conn.commit()
            conn.close()

            vs = VectorStore(
                db_path=str(vec_db),
                embed_mode="local",
                local_model_dir=str(MODEL_DIR),
            )
            with pytest.raises(RuntimeError, match="rebuild_vec_local"):
                await vs.init()
            await vs.close()

    asyncio.run(run())
