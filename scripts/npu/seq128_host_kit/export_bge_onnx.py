"""bge-large-zh-v1.5 → 固定 seq=128 ONNX 导出（x86 host 执行）。

在 host 侧 venv 里运行（依赖 torch/transformers/onnx，见
docs/NPU_NBG_SEQ128_RUNBOOK.md §1.3、§2）：

    python export_bge_onnx.py \
        --model-dir /path/to/bge-large-zh-v1.5 \
        --seq 128 \
        --out bge_large_zh_seq128.onnx

说明：
- 输出 last_hidden_state (B,S,H)，CLS 池化 + L2 归一化由板端 runner 完成，
  与现行 512 包约定一致（scripts/npu/bge_npu_runner.c extract_cls）。
- 固定 shape（dynamic_axes=None）：ACUITY 对静态图支持最稳；bge-large 2 有效输入
  + token_type_ids 全 0 第三输入（对齐旧校准集三元组格式，数值无影响）。
- opset 14：ACUITY 6.30 支持良好；更高 opset 可能引入不受支持的 LayerNorm 变体算子。
"""
from __future__ import annotations

import argparse

import torch
from transformers import AutoModel, AutoTokenizer


class Wrapper(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self._model = model

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        token_type_ids: torch.Tensor | None = None,
    ) -> torch.Tensor:
        out = self._model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
        )
        return out.last_hidden_state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--seq", type=int, default=128)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out_path = args.out or f"bge_large_zh_seq{args.seq}.onnx"

    tok = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModel.from_pretrained(args.model_dir).eval()

    wrapper = Wrapper(model)
    dummy_inputs = (
        torch.ones(1, args.seq, dtype=torch.long),
        torch.ones(1, args.seq, dtype=torch.long),
        torch.zeros(1, args.seq, dtype=torch.long),
    )
    torch.onnx.export(
        wrapper,
        dummy_inputs,
        out_path,
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"],
        opset_version=14,
        do_constant_folding=True,
        dynamic_axes=None,
    )

    # 自检：onnxruntime 与 transformers 原始输出对拍
    try:
        import numpy as np
        import onnxruntime as ort

        text = "帮我写一个python脚本处理excel数据"
        enc = tok(text, return_tensors="pt", truncation=True, max_length=args.seq)
        with torch.no_grad():
            ref = model(**enc).last_hidden_state[0, 0].numpy()  # CLS
        sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
        feed = {
            "input_ids": enc["input_ids"].numpy(),
            "attention_mask": enc["attention_mask"].numpy(),
            "token_type_ids": enc.get("token_type_ids", torch.zeros_like(enc["input_ids"])).numpy(),
        }
        got = sess.run(None, feed)[0][0, 0]
        cos = float(np.dot(ref, got) / (np.linalg.norm(ref) * np.linalg.norm(got)))
        print(f"self-check cosine(CLS) = {cos:.4f} (expect >= 0.999)")
        if cos < 0.999:
            raise SystemExit("FAILED: export accuracy regression")
    except ImportError as e:
        print(f"self-check skipped ({e})")

    print("exported:", out_path)


if __name__ == "__main__":
    main()
