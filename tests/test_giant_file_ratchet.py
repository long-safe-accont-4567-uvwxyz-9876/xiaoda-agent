"""巨型文件止血守卫（ratchet）：热点文件只许拆小，不许继续堆肥。

背景（docs/tech_debt_audit_2026-08-25.md §二 / giant_files_split_plan_2026-08-22）：
qq_bot_adapter 在 P4 基类沉淀（eba85d91）后不降反涨 354 行——说明"结构性拆分"
挡不住日常迭代往单文件本体堆逻辑。本守卫把四个最高风险热点的行数钉在
当前实测基线上：任何使其变长的改动必须在同一提交里给出拆分或同步下调基线。

规则：
1. 文件行数 ≤ BASELINE 记录值。缩小是好事，允许（基线随后续净修复下调）；
2. 超限失败信息必须指向"拆分或下调基线"，防止机械 +1 绕过；
3. 基线只许随真实拆分/删码下调，与 broad_except 棘轮同纪律。

已知边界：本守卫只看总行数这一个代理指标，不判断代码好坏——它的目标不是
"让数字变小"，而是在有人往 2000+ 行的适配器本体再塞一个方法时，
强制其停下来想一想该进基类、子模块还是新文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 基线 = 各文件止血液轮落地时实测值（wc -l）。
# 下调流程：拆分/删码合入后，把这里改成新的实测值并在提交说明注明。
# 2026-08-25 P2 拆分①：prompt_builder.py(1669) → 包(门面148/common50/
#   scene781/assembly661/workspace215)，原条目替换为包内两大子模块。
# 2026-08-25 P2 拆分②：web/ws_hub.py(1591) → ws_hub(1118)+ws_terminal(518)，
#   原条目替换为拆分后两文件实测值。
BASELINES: dict[str, int] = {
    "qq_bot_adapter.py": 2174,
    "wechat_bot_adapter.py": 1584,
    "web/ws_hub.py": 1118,
    "web/ws_terminal.py": 534,
    "prompt_builder/_prompt_scene.py": 781,
    "prompt_builder/_prompt_assembly.py": 661,
}


def _line_count(rel: str) -> int:
    return len((ROOT / rel).read_text(encoding="utf-8").splitlines())


def test_hotspot_files_do_not_grow():
    overgrown = [
        f"{rel}: {_line_count(rel)} > baseline {limit}"
        for rel, limit in BASELINES.items()
        if _line_count(rel) > limit
    ]
    assert not overgrown, (
        "巨型文件止血液轮被突破——这些文件比基线更大：\n  "
        + "\n  ".join(overgrown)
        + "\n处理方式（二选一）：\n"
        "  a) 把新增逻辑放进 channel_adapter_base / 子模块 / 新文件，而不是本体外加；\n"
        "  b) 确属必要的本体增长（如临时修复），同步下调 BASELINES 前先开拆分 issue。"
    )


def test_baseline_matches_reality_when_smaller():
    """基线若大于现状（文件已被拆小），提示下调基线但不失败——保持棘轮单调向下。"""
    stale = [
        f"{rel}: baseline {limit} > actual {_line_count(rel)}"
        for rel, limit in BASELINES.items()
        if _line_count(rel) < limit - 50  # 容忍 ±50 行内的自然波动噪音
    ]
    # 不 fail：仅当明显缩小时打印提醒。真正的棘轮下调由人工在拆分提交里完成。
    if stale:
        print("建议下调 BASELINES（文件已显著变小）:\n  " + "\n  ".join(stale))


if __name__ == "__main__":
    sys.exit(0 if not any(_line_count(rel) > limit for rel, limit in BASELINES.items()) else 1)
