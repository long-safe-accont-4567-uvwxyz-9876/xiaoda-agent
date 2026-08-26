"""P0-1 memory_type 分类质量离线评估（按需运行，不入 CI）。

用法：
    .venv/bin/python -m evaluation.eval_memory_type_classification [--limit 20] [--task-type memory_encoding]

需要真实模型 provider（与生产 memory_encoding 槽位一致的 env 配置）。
prompt 与生产 _enrich_memory_async 共用 build_classification_prompt 单一事实源，
解析走生产严格 parser（parse_memory_enrichment），解析失败/fallback 计为错误。

验收（规格 v1.1）：整体准确率 ≥ 90% 退出码 0，否则 1。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from memory._memory_encoder import MemoryEncoder
from memory.enrichment import MEMORY_TYPES, build_classification_prompt, parse_memory_enrichment

DEFAULT_DATASET = Path(__file__).parent / "datasets" / "memory_type_classification_golden.json"
TARGET_ACCURACY = 0.90


def load_dataset(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["items"]
    for item in items:
        assert item["expected"] in MEMORY_TYPES, f"非法标签: {item['id']}"
        assert item["exchanges"], f"空 exchanges: {item['id']}"
    return items


async def evaluate(items: list[dict], task_type: str) -> int:
    from model_router import ModelRouter

    router = ModelRouter()
    confusion: Counter = Counter()
    errors: list[str] = []
    correct = 0

    for index, item in enumerate(items, 1):
        text = MemoryEncoder._build_enrichment_text(item["exchanges"])
        prompt = build_classification_prompt(text)
        try:
            raw = await router.route(
                task_type,
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
            )
            parsed = parse_memory_enrichment(raw)
            predicted = parsed.memory_type
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断评估
            predicted = f"<error:{type(exc).__name__}>"
            errors.append(f"{item['id']}: {exc}")

        expected = item["expected"]
        confusion[(expected, predicted)] += 1
        if predicted == expected:
            correct += 1
        print(f"[{index}/{len(items)}] {item['id']} expected={expected} predicted={predicted}")

    total = len(items)
    accuracy = correct / total if total else 0.0
    print("\n=== 混淆矩阵（行=期望 列=预测）===")
    headers = sorted(MEMORY_TYPES)
    print("期望\\预测\t" + "\t".join(headers))
    for expected in headers:
        row = [str(confusion[(expected, p)] or ".") for p in headers]
        bad = sum(v for (e, p), v in confusion.items() if e == expected and p != expected)
        print(f"{expected}\t" + "\t".join(row) + f"\t(错分 {bad})")

    if errors:
        print(f"\n解析/调用错误 {len(errors)} 条：")
        for line in errors[:10]:
            print(f"  {line}")

    print(f"\n整体准确率: {correct}/{total} = {accuracy:.1%}（目标 ≥ {TARGET_ACCURACY:.0%}）")
    return 0 if accuracy >= TARGET_ACCURACY else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=0, help="只评估前 N 条（0=全部）")
    parser.add_argument("--task-type", default="memory_encoding")
    args = parser.parse_args()

    items = load_dataset(args.dataset)
    if args.limit:
        items = items[: args.limit]
    return asyncio.run(evaluate(items, args.task_type))


if __name__ == "__main__":
    sys.exit(main())
