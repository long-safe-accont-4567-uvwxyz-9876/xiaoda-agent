"""校准集生成：文本清单 → ACUITY dataset.txt + (1,SEQ) int32 npy 三元组。

host 侧执行（依赖 tokenizers/numpy，见 docs/NPU_NBG_SEQ128_RUNBOOK.md §1.3、§3）：

    python make_calib.py \
        --tokenizer /path/to/bge-large-zh-v1.5/tokenizer.json \
        --texts calib_texts.txt \
        --seq 128 \
        --out calib128

校准语料建议直接取生产真实分布（板端示例）：
    sqlite3 agent.db "SELECT summary FROM episodic_memories ORDER BY id DESC LIMIT 500" > calib_texts.txt

输出目录结构（喂 convert_onnx_to_nbg.sh --dataset）：
    calib128/
    ├── dataset.txt                    # 每行: input_ids attention_mask token_type_ids
    ├── sample_000_input_ids.npy       # (1,SEQ) int32
    ├── sample_000_attention_mask.npy  # (1,SEQ) int32
    └── sample_000_token_type_ids.npy  # (1,SEQ) int32 全 0
"""
from __future__ import annotations

import argparse
import pathlib

import numpy as np
from tokenizers import Tokenizer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", required=True, help="tokenizer.json 路径")
    ap.add_argument("--texts", required=True, help="每行一条的校准文本清单")
    ap.add_argument("--seq", type=int, default=128)
    ap.add_argument("--out", default="calib128")
    ap.add_argument("--max-samples", type=int, default=200,
                    help="ACUITY 校准 100~300 条即可；清单更多时均匀抽样")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tok = Tokenizer.from_file(args.tokenizer)
    seq = args.seq

    texts = [line.strip() for line in open(args.texts, encoding="utf-8") if line.strip()]
    if len(texts) > args.max_samples:
        step = len(texts) / args.max_samples
        texts = [texts[int(i * step)] for i in range(args.max_samples)]
        print(f"sampling {args.max_samples}/{len(open(args.texts, encoding='utf-8').readlines())} lines")

    lines: list[str] = []
    for text in texts:
        enc = tok.encode(text)
        ids = list(enc.ids[:seq])
        # tokenizers 包的 Encoding 无 attention 数组属性；special tokens
        # mask 恒为 0（BGE 不加额外特殊位），真实掩码用"非 pad token"即可。
        mask = [1] * len(ids)
        pad = seq - len(ids)
        ii = np.array(ids + [0] * pad if ids else [101] + [0] * (seq - 1), dtype=np.int32)[:seq]
        mm = np.array(mask + [0] * pad if mask else [1] + [0] * (seq - 1), dtype=np.int32)[:seq]
        tt = np.zeros(seq, dtype=np.int32)

        stem = f"sample_{len(lines):03d}"
        for name, arr in (
            ("input_ids", ii),
            ("attention_mask", mm),
            ("token_type_ids", tt),
        ):
            np.save(out / f"{stem}_{name}.npy", arr.reshape(1, seq))
        lines.append(
            f"{stem}_input_ids.npy {stem}_attention_mask.npy {stem}_token_type_ids.npy"
        )

    (out / "dataset.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"{len(lines)} samples -> {out}/dataset.txt")


if __name__ == "__main__":
    main()
