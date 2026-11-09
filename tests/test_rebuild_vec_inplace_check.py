"""rebuild_vec_inplace 的默认水位与自检查 top1 并列口径。

2026-11-09 背景：
- DEFAULT_MAX_ROWS=5000 低于已压到的热区 8000，任何不带 --no-auto-compress 的运行
  都会先把热表压到 --hot-low=4000，属不可逆下沉；
- 自检 memories_child_vec 的 top1 16/20 不是向量写坏，而是同文本重复子块等距并列，
  vec0 返回哪一行属于实现细节。口径只允许「文本逐字相同 + 距离在并列误差内」。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import sqlite_vec

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "rebuild_vec_inplace_under_test", ROOT / "scripts" / "rebuild_vec_inplace.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


class _FakeSvc:
    """文本→向量的确定性替身，避开本地模型加载。"""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._mapping[t] for t in texts]


def _build(tmp_path, rows: list[tuple[int, str]], vectors: dict[str, list[float]]):
    adb = sqlite3.connect(tmp_path / "agent.db")
    adb.execute("CREATE TABLE memory_child_chunks (id INTEGER PRIMARY KEY, embed_content TEXT)")
    adb.executemany("INSERT INTO memory_child_chunks VALUES (?, ?)", rows)
    adb.commit()

    conn = sqlite3.connect(tmp_path / "agent_vec.db")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("CREATE VIRTUAL TABLE memories_child_vec USING vec0(embedding float[4])")
    conn.execute(
        "CREATE TABLE vec_rebuild_progress ("
        "vec_table TEXT, source_id INTEGER, done_at TEXT, PRIMARY KEY (vec_table, source_id))"
    )
    for rid, text in rows:
        conn.execute(
            "INSERT INTO memories_child_vec(rowid, embedding) VALUES (?, vec_f32(?))",
            (rid, sqlite_vec.serialize_float32(vectors[text])),
        )
        conn.execute(
            "INSERT INTO vec_rebuild_progress VALUES ('memories_child_vec', ?, '2026-11-09T00:00:00')",
            (rid,),
        )
    conn.commit()
    return conn, adb


def test_default_watermark_matches_hot_target() -> None:
    assert MOD.HOT_TARGET_ROWS == 8000
    assert MOD.DEFAULT_MAX_ROWS == MOD.HOT_TARGET_ROWS
    assert MOD.DEFAULT_MAX_ROWS >= 8000


def test_guard_is_silent_when_default_covers_hot_area() -> None:
    assert MOD._default_watermark_guard(8000, 8000, explicit=False) is None
    assert MOD._default_watermark_guard(7000, 8000, explicit=False) is None


def test_guard_warns_when_default_below_hot_area() -> None:
    msg = MOD._default_watermark_guard(9000, 8000, explicit=False)
    assert msg is not None
    assert "8000" in msg and "9000" in msg
    assert "--no-auto-compress" in msg
    assert "--hot-low" in msg


def test_guard_is_silent_when_watermark_explicit() -> None:
    assert MOD._default_watermark_guard(9000, 8000, explicit=True) is None


def test_tie_pass_requires_identical_text_and_distance() -> None:
    assert MOD._tie_pass(
        query_text="实体: 小妲", top1_text="实体: 小妲", top1_dist=0.0, self_dist=0.0
    )
    # 文本相同但距离差超出并列误差：不属于并列
    assert not MOD._tie_pass(
        query_text="实体: 小妲", top1_text="实体: 小妲", top1_dist=0.3, self_dist=0.0
    )
    # 距离并列但文本不同：仍算 top1 失败
    assert not MOD._tie_pass(
        query_text="实体: 小妲", top1_text="实体: 小妲（旧）", top1_dist=0.0, self_dist=0.0
    )
    assert not MOD._tie_pass(
        query_text="实体: 小妲", top1_text=None, top1_dist=0.0, self_dist=0.0
    )


def test_check_passes_with_duplicate_text_tie(tmp_path, capsys) -> None:
    vectors = {
        "重复子块": [1.0, 0.0, 0.0, 0.0],
        "另一条": [0.0, 1.0, 0.0, 0.0],
        "第三条": [0.0, 0.0, 1.0, 0.0],
        "第四条": [0.0, 0.0, 0.0, 1.0],
        "第五条": [0.5, 0.5, 0.0, 0.0],
    }
    rows = [
        (1, "重复子块"),
        (2, "重复子块"),
        (3, "另一条"),
        (4, "第三条"),
        (5, "第四条"),
        (6, "第五条"),
    ]
    conn, adb = _build(tmp_path, rows, vectors)
    try:
        rc = MOD._check(conn, adb, _FakeSvc(vectors), ["memories_child_vec"])
    finally:
        conn.close()
        adb.close()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "等距并列=" in out
    tied = int(out.split("等距并列=")[1].split()[0])
    assert tied >= 1, out


def test_check_still_fails_when_tie_text_differs(tmp_path, capsys) -> None:
    vectors = {"文本A": [1.0, 0.0, 0.0, 0.0], "文本B": [1.0, 0.0, 0.0, 0.0]}
    rows = [(1, "文本A"), (2, "文本B")]
    conn, adb = _build(tmp_path, rows, vectors)
    try:
        rc = MOD._check(conn, adb, _FakeSvc(vectors), ["memories_child_vec"])
    finally:
        conn.close()
        adb.close()
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "FAIL" in out
