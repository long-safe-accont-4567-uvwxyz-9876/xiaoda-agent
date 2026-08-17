#!/usr/bin/env python3
"""NPU(NBG INT16) vs CPU 检索质量验证。

对同一组真实记忆文本：
- CPU：local_embed（原始 GELU fp32 模型）生成文档向量（动态长度，模拟业务库行为）
- CPU query：pad 到 512 固定长度（与 NPU 同输入，公平对比）
- NPU query：vpm_run 跑 bge_small_zh.nb（INT16 sigmoid）→ CLS 池化 + L2 归一化

指标：
- query 向量一致性（CPU512 vs NPU 余弦）
- top-k 检索结果重合率（CPU 检索 vs NPU 检索）
- 文档向量抽样一致性（NPU 生成文档向量 vs CPU，验证库重建后用 NPU 也可行）

用法：python scripts/bench_npu_retrieval.py [--docs 100] [--queries 10] [--k 10]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.local_embed import LocalEmbeddingProvider  # noqa: E402

_DATA_DIR = os.getenv("KIOXIA_DATA_DIR", "") or str(Path.home() / ".ai-agent" / "data")
MODEL = Path(os.getenv("LOCAL_EMBED_MODEL_DIR", "") or str(Path(_DATA_DIR) / "models" / "bge-small-zh-v1.5"))
ADB = str(Path(_DATA_DIR) / "db" / "agent.db")
NPU_DIR = Path(_DATA_DIR) / "npu" / "bge_npu_kit" / "npu_input"
VPM = "/opt/vpm_run/vpm_run"
SEQ = 512
SCALE, ZP = 0.031434, 181  # INT16 输出量化参数（vpm_run 打印）


def _norm(vecs: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(vecs, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return vecs / n


def _run_npu(ids: list[int], mask: list[int], tt: list[int],
             workdir: Path, tag: str) -> np.ndarray:
    """vpm_run 推理一条固定 512 输入，返回 (512,512) 反量化输出。"""
    for name, data in (("input_ids.dat", ids), ("attention_mask.dat", mask),
                       ("token_type_ids.dat", tt)):
        (workdir / name).write_bytes(np.array(data, dtype=np.int32).tobytes())
    sample = workdir / "sample_tmp.txt"
    sample.write_text(
        "[network]\n./bge_small_zh.nb\n"
        "[input]\n./input_ids.dat\n./attention_mask.dat\n./token_type_ids.dat\n"
    )
    out_txt = workdir / "output_0.txt"
    out_txt.unlink(missing_ok=True)
    r = subprocess.run(
        ["sudo", VPM, "-s", str(sample), "-l", "1", "-b", "0", "--save_txt", "1"],
        capture_output=True, text=True, timeout=60, cwd=str(workdir),
    )
    if r.returncode != 0 or not out_txt.exists():
        raise RuntimeError(f"vpm_run 失败 tag={tag} rc={r.returncode}\n{r.stderr[-500:]}")
    vals = np.fromfile(out_txt, sep="\n", dtype=np.float32)
    arr = vals.reshape(SEQ, SEQ)
    (workdir / f"npu_{tag}.txt").write_bytes(out_txt.read_bytes())
    return arr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=100)
    ap.add_argument("--queries", type=int, default=10)
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(ADB)
    rows = con.execute(
        "SELECT summary FROM episodic_memories "
        "WHERE summary IS NOT NULL AND trim(summary) != '' "
        "ORDER BY id DESC LIMIT ?", (args.docs + args.queries + 20,)
    ).fetchall()
    con.close()
    texts = [r[0] for r in rows]
    docs = texts[: args.docs]
    queries = texts[args.docs: args.docs + args.queries]
    print(f"[info] 文档={len(docs)} query={len(queries)} k={args.k}", flush=True)

    provider = LocalEmbeddingProvider(MODEL, query_prefix="")
    if not provider.load():
        print(f"[error] 模型加载失败: {provider._load_error}")
        return 1

    # ── 文档向量：CPU 动态长度（业务库行为） ──
    doc_vecs = np.array(provider.encode_batch(docs), dtype=np.float32)
    doc_vecs = _norm(doc_vecs)
    print(f"[info] 文档向量 OK {doc_vecs.shape}", flush=True)

    # ── tokenize 固定 512（CPU 与 NPU 同输入） ──
    tok = provider._tokenizer
    encs = tok.encode_batch(queries, add_special_tokens=True)
    ids, mask, tt = [], [], []
    for e in encs:
        i = e.ids[:SEQ]
        m = [1] * len(i) + [0] * (SEQ - len(i))
        t = list(e.type_ids[:SEQ]) + [0] * (SEQ - len(e.type_ids))
        ids.append(i + [0] * (SEQ - len(i)))
        mask.append(m)
        tt.append(t)
    ids_a = np.array(ids, dtype=np.int64)
    mask_a = np.array(mask, dtype=np.int64)
    tt_a = np.array(tt, dtype=np.int64)

    # ── CPU query（固定 512） ──
    out = provider._session.run(None, {
        "input_ids": ids_a, "attention_mask": mask_a, "token_type_ids": tt_a,
    })[0]
    cpu_q = _norm(provider._pool_cls(out))

    # ── NPU query ──
    NPU_DIR.mkdir(parents=True, exist_ok=True)
    npu_q = np.zeros((len(queries), 512), dtype=np.float32)
    for qi in range(len(queries)):
        arr = _run_npu(ids[qi], mask[qi], tt[qi], NPU_DIR, f"q{qi}")
        npu_q[qi] = _norm(arr[0:1, :])[0]
        print(f"[info] NPU query {qi} OK", flush=True)
    npu_q = _norm(npu_q)

    # ── 指标 ──
    # 1) query 一致性
    cos_q = np.array([
        float(np.dot(cpu_q[i], npu_q[i])) for i in range(len(queries))
    ])
    print(f"\n[指标] CPU512 vs NPU query 余弦: mean={cos_q.mean():.4f} "
          f"min={cos_q.min():.4f}", flush=True)

    # 2) 检索一致性
    def topk(q, k):
        sim = doc_vecs @ q
        return set(np.argsort(-sim)[:k].tolist())

    hits5 = hits10 = 0
    for i in range(len(queries)):
        c5, n5 = topk(cpu_q[i], 5), topk(npu_q[i], 5)
        c10, n10 = topk(cpu_q[i], 10), topk(npu_q[i], 10)
        hits5 += len(c5 & n5) / 5
        hits10 += len(c10 & n10) / 10
    print(f"[指标] top-5 重合率: {hits5 / len(queries):.4f}", flush=True)
    print(f"[指标] top-10 重合率: {hits10 / len(queries):.4f}", flush=True)

    # 3) 文档向量一致性抽样（前 5 条文档跑 NPU 对比 CPU）
    print("\n[抽样] 文档 NPU vs CPU 一致性（前5条）:", flush=True)
    for di in range(min(5, args.docs)):
        e = tok.encode_batch([docs[di]], add_special_tokens=True)[0]
        i = e.ids[:SEQ]
        m = [1] * len(i) + [0] * (SEQ - len(i))
        t = list(e.type_ids[:SEQ]) + [0] * (SEQ - len(e.type_ids))
        ids1 = i + [0] * (SEQ - len(i))
        arr = _run_npu(ids1, m, t, NPU_DIR, f"d{di}")
        npu_doc = _norm(arr[0:1, :])[0]
        c = float(np.dot(doc_vecs[di], npu_doc))
        print(f"  文档#{di} cos={c:.4f}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
