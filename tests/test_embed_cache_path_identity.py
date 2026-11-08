"""审计 M3：embedding 缓存文件名必须带上本地模型目录身份。

背景（2026-11-08 只读实测）：memory/vector_store.py 的 _embed_cache_path()
只按 self._embed_model 命名。本地模式下 _embed_model 是逻辑名（如 bge-m3），
真正决定向量空间的是 LOCAL_EMBED_MODEL_DIR；换成本地目录里同维度的另一个
模型时文件名不变，会静默复用旧向量（检索结果错但不报错）。
"""
from __future__ import annotations

from pathlib import Path

from memory.vector_store import VectorStore


def _store(tmp_path, local_dir: str) -> VectorStore:
    vs = VectorStore.__new__(VectorStore)  # 只测命名，不建索引/连接
    vs._embed_model = "bge-m3"
    vs._db_path = str(tmp_path / "agent.db")
    vs._local_model_dir = local_dir
    return vs


def test_remote_mode_filename_unchanged(tmp_path):
    """非本地模式（无本地目录）文件名与旧版一致，不产生迁移成本。"""
    vs = _store(tmp_path, "")
    assert Path(vs._embed_cache_path()).name == "embed_cache_bge-m3.npz"


def test_local_model_dir_changes_filename(tmp_path):
    vs_remote = _store(tmp_path, "")
    vs_local = _store(tmp_path, str(tmp_path / "models" / "bge-m3-ft"))
    local_name = Path(vs_local._embed_cache_path()).name
    assert local_name != Path(vs_remote._embed_cache_path()).name
    assert local_name == "embed_cache_bge-m3__bge-m3-ft.npz"


def test_two_local_dirs_do_not_share_cache(tmp_path):
    a = _store(tmp_path, str(tmp_path / "m" / "model-a"))
    b = _store(tmp_path, str(tmp_path / "m" / "model-b"))
    assert a._embed_cache_path() != b._embed_cache_path()
