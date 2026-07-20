#!/usr/bin/env python3
"""检查 PR 覆盖率下降幅度, 输出 GitHub Actions warning / notice.

用于 ci-tests.yml 的 "Coverage drop warning" 步骤, 替代原先内嵌在 `run: |`
块中的 `python -c "..."` 多行代码 —— 后者会因缩进为 0 破坏 YAML literal
block scalar, 导致整个 ci-tests.yml 无法解析.

用法:
    python scripts/check_coverage_drop.py <ref_coverage> <new_coverage>

参数:
    ref_coverage: 基线覆盖率 (str, 百分数, 如 "85.5")
    new_coverage: 当前 PR 覆盖率 (str, 百分数, 如 "83.2")

退出码:
    0 - 始终为 0 (warning 级别不阻塞 CI, 配合 continue-on-error: true)

输出 (stdout):
    ::warning::...     — 当覆盖率下降 > 2 个百分点
    ::notice::...      — 当覆盖率下降 0~2 个百分点 (容差内) 或提升
    Coverage evolution: ref% -> new% (diff: +X.XX%)
"""
from __future__ import annotations

import math
import sys


# 覆盖率下降容忍阈值 (百分点). 超过此值才输出 ::warning::.
_DROP_WARNING_THRESHOLD = -2.0


def _is_blank(value) -> bool:
    """判断值是否为空字符串或仅空白 (None 不视为 blank).

    None 不视为 blank —— 它代表调用方传了无效值 (非 CI env var 缺失场景),
    应该由 _parse_coverage 输出 ::warning:: 提示开发者.

    Args:
        value: 任意输入值

    Returns:
        True 表示该值视为缺失, 应静默跳过比较 (CI env var 未设的合法场景)
    """
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _parse_coverage(value, name: str) -> float | None:
    """将覆盖率字符串解析为 float, 拒绝 NaN / Inf.

    Args:
        value: 输入值 (str / None / 其他)
        name: 字段名 (用于错误消息, "ref" / "new")

    Returns:
        解析后的 float; 无效时返回 None (调用方据此输出 ::warning::)
    """
    try:
        parsed = float(value)
    except (ValueError, TypeError) as e:
        print(
            f"::warning::Invalid coverage value {name}={value!r} ({e})"
        )
        return None
    # float("NaN") / float("inf") 会成功返回, 需额外校验
    if math.isnan(parsed) or math.isinf(parsed):
        print(
            f"::warning::Invalid coverage value {name}={value!r} "
            f"(NaN or Inf not allowed)"
        )
        return None
    return parsed


def check_coverage_drop(ref_str, new_str) -> int:
    """比较覆盖率, 输出 GitHub Actions annotation.

    Args:
        ref_str: 基线覆盖率字符串 (如 "85.5"); 空串/仅空白静默返回 0,
                 None 输出 ::warning:: (异常调用)
        new_str: 当前覆盖率字符串; 同上

    Returns:
        始终返回 0 (warning 不阻塞 CI, 由 continue-on-error 兜底)
    """
    # None / 空字符串 / 仅空白都视为缺失基准值, 静默跳过 (不输出 annotation)
    if _is_blank(ref_str) or _is_blank(new_str):
        return 0

    ref = _parse_coverage(ref_str, "ref")
    new = _parse_coverage(new_str, "new")
    if ref is None or new is None:
        # _parse_coverage 已经输出了 ::warning::, 直接返回
        return 0

    diff = round(new - ref, 2)
    print(f"Coverage evolution: {ref}% -> {new}% (diff: {diff:+.2f}%)")

    if diff < _DROP_WARNING_THRESHOLD:
        print(
            f"::warning::Coverage dropped by {abs(diff):.2f}% "
            f"(from {ref}% to {new}%), exceeding the 2% threshold"
        )
    elif diff < 0:
        print(
            f"::notice::Coverage dropped by {abs(diff):.2f}% "
            f"(within 2% tolerance)"
        )
    else:
        print(f"::notice::Coverage improved by {diff:.2f}%")

    return 0


def _main(argv: list[str]) -> int:
    """CLI 入口.

    Args:
        argv: 命令行参数 (不含 argv[0])

    Returns:
        进程退出码 (0 成功, 2 参数错误)
    """
    if len(argv) != 2:
        print(
            f"Usage: {sys.argv[0]} <ref_coverage> <new_coverage>",
            file=sys.stderr,
        )
        return 2
    return check_coverage_drop(argv[0], argv[1])


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
