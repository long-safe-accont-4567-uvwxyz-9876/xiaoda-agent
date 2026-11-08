"""审计 M3（遗留）：EmbedCache 身份/形状守卫——换模型必须整体失效。"""
from __future__ import annotations

from memory.vector_store import EmbedCache


def _cache(tmp_path, identity: str) -> EmbedCache:
    return EmbedCache(max_size=8, persist_path=str(tmp_path / "embed_cache_x.npz"),
                      identity=identity)


def test_same_identity_roundtrip(tmp_path):
    c1 = _cache(tmp_path, "bge-m3|models/bge-m3")
    c1._cache["k1"] = [0.1, 0.2, 0.3]
    c1._save_persisted()

    c2 = _cache(tmp_path, "bge-m3|models/bge-m3")
    assert list(c2._cache.keys()) == ["k1"]


def test_identity_mismatch_discards_cache(tmp_path):
    c1 = _cache(tmp_path, "bge-m3|models/bge-m3")
    c1._cache["k1"] = [0.1, 0.2, 0.3]
    c1._save_persisted()

    c2 = _cache(tmp_path, "bge-m3|models/other-model")
    assert c2._cache == {}


def test_legacy_npz_without_identity_still_loads(tmp_path):
    """旧版本 npz 没有 identity 字段：允许加载（文件名已是身份），不误伤。"""
    import numpy as np

    path = tmp_path / "embed_cache_old.npz"
    np.savez(path, keys=np.array(["k"]), vecs=np.array([[0.1, 0.2]], dtype=np.float32))
    c = EmbedCache(max_size=8, persist_path=str(path), identity="bge-m3|models/bge-m3")
    assert list(c._cache.keys()) == ["k"]


def test_ragged_npz_rejected(tmp_path):
    """keys 与 vecs 行数不一致（文件被截断）时整体失效，避免错位命中。"""
    import numpy as np

    path = tmp_path / "embed_cache_bad.npz"
    np.savez(path, keys=np.array(["a", "b"]), vecs=np.array([[0.1, 0.2]], dtype=np.float32))
    c = EmbedCache(max_size=8, persist_path=str(path), identity="id")
    assert c._cache == {}
