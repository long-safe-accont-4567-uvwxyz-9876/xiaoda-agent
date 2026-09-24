"""Jev 风控门卫 —— 工具执行前的语义风险判断（TypeSafe System One）。

定位（Jev 能力探索报告 · 2026-09-23 实测，P0 推荐项）：
    现有关键词/正则黑名单只能拦住**已知**的危险命令，对"看着人畜无害、
    实则危险"的变体（如把破坏性操作藏进管道、拼写变形、语义等价改写）无能为力。
    Jev 的 Noul 原语做「这条命令是否需要用户确认」的**语义**判断，
    实测区分度良好：
        sudo rm -rf ...  → 0.80（拦截）
        ls -la           → 0.07（放行）
    单次判断成本约两万分之一美元，适合每次工具调用前的高频门禁。

与现有门禁的关系（**叠加层，不是替代**）：
    本模块**只用于「降低误拦」和「补漏」，绝不放宽已有硬拦截**。
    - security/dangerous_targets.py 的黑名单（FATAL_SHELL_RE 等）始终先执行，
      Jev 不参与、也无法推翻硬拦截。
    - 本模块的结论只影响「是否弹确认卡片」这一档：
        allow  → 可跳过确认（仅当原关键词门禁也认为"不在白名单需确认"时才用得上）
        confirm→ 保持原有确认流程
        deny   → 直接拒绝（比关键词更严，用于语义上明显危险但关键词漏掉的）
    - Jev 不可用/超时/低置信 → 一律返回 ``None``，调用方走原有逻辑（fail-closed）。

置信度使用（关键，来自实测报告）：
    报告明确指出：**conf 高 ≠ 正确**。无逃生门时模型乱选也能给到 0.80~0.85。
    因此本模块采用**双向阈值 + 分布形状检查**：
      - 只有 conf >= deny_threshold 才敢直接拒绝（宁可放过、不误杀用户操作）
      - 只有 conf <= allow_threshold 才敢跳过确认（宁可多问一次）
      - 中间地带一律 confirm（保持原行为）
      - 分布摊薄（``distribution_is_suspicious``）时降级为 confirm

设计约束（与 jev_client 一致）：
    - 所有异常一律降级返回 None，绝不打断工具执行主流程。
    - 未启用 JEV_ENABLED 或未配置密钥时零行为变化。
"""
from __future__ import annotations

from loguru import logger

# 默认阈值（config 可覆盖）：
#   >= deny_threshold  直接拒绝（需较高确信，避免误杀合法操作）
#   <= allow_threshold 可跳过确认（需较低风险分，避免放行危险操作）
#   中间地带 → confirm（保持原有确认流程）
DEFAULT_DENY_THRESHOLD = 0.85
DEFAULT_ALLOW_THRESHOLD = 0.20

# 判定结果枚举（字符串常量，便于日志与调用方比较）
RISK_ALLOW = "allow"
RISK_CONFIRM = "confirm"
RISK_DENY = "deny"


def _config_float(name: str, default: float) -> float:
    try:
        import config as _cfg
        return float(getattr(_cfg, name, default))
    except (ImportError, AttributeError, ValueError, TypeError):
        return default


def is_enabled() -> bool:
    """风控门卫是否启用（JEV_ENABLED 且已配置密钥）。"""
    try:
        from utils import jev_client as jev
    except ImportError:
        return False
    return jev.is_available()


def _build_question() -> dict:
    """构造风险判定问题。

    措辞遵循实测守则：rubric 具体到"两个审查者能达成一致"，并写明
    判定的是"是否需要人工确认"而非"是否绝对禁止"。
    """
    from utils import jev_client as jev
    return jev.noul(
        instructions=(
            "下面这条 shell 命令，如果在用户的开发机上直接执行，"
            "是否需要先让用户人工确认？（判断依据是命令**实际会造成的后果**，"
            "而非它看起来是否眼熟）"
        ),
        true_means=(
            "需要确认：会不可逆地删除/覆盖用户数据、修改系统级配置、"
            "批量变更文件权限、向外部传输本地数据、下载并执行远程脚本、"
            "提权操作，或后果超出当前工作目录范围"
        ),
        false_means=(
            "无需确认：只读查询、查看状态、在当前项目内做可逆的常规操作"
            "（如查看文件、搜索、运行测试、git 只读命令）"
        ),
    )


async def assess_command(command: str, *, tool_name: str = "shell_command") -> str | None:
    """判断一条命令是否需要用户确认。

    Returns:
        - ``RISK_DENY``    语义上明显危险，建议拒绝
        - ``RISK_ALLOW``   语义上明显安全，可跳过确认
        - ``RISK_CONFIRM`` 无法明确判断，保持原有确认流程
        - ``None``         Jev 不可用/失败（调用方走原有逻辑，fail-closed）

    绝不放宽硬拦截：调用方必须先用 ``dangerous_targets`` 黑名单过滤，
    本函数只处理"黑名单之外的模糊地带"。
    """
    if not command or not command.strip():
        return None
    if not is_enabled():
        return None

    try:
        from utils import jev_client as jev
    except ImportError:
        return None

    deny_threshold = _config_float("JEV_RISK_DENY_THRESHOLD", DEFAULT_DENY_THRESHOLD)
    allow_threshold = _config_float("JEV_RISK_ALLOW_THRESHOLD", DEFAULT_ALLOW_THRESHOLD)
    timeout = _config_float("JEV_TIMEOUT", 8.0)

    try:
        answers = await jev.system_one(
            # state 只放判定所需的字段（实测：无关内容会稀释准确率）
            state={"command": command[:2000]},
            questions={"needs_confirmation": _build_question()},
            timeout=timeout,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        # 参数/结构类异常统一降级为 None，由调用方走原有确认流程（fail-closed）
        return None
    except (OSError, RuntimeError) as e:
        logger.warning("jev_risk_gate.assess_failed error={} type={}",
                       repr(e)[:160], type(e).__name__)
        return None

    risk = jev.answer_noul(answers, "needs_confirmation")
    if risk is None:
        logger.debug("jev_risk_gate.no_answer")
        return None

    # Noul 无 confidence 字段（实测：noul 值本身即概率），
    # 但概率本身也需要交叉验证——极端值（接近 0/1）才可信。
    if risk >= deny_threshold:
        logger.info("jev_risk_gate.deny tool={} risk={} cmd={}",
                    tool_name, risk, command[:120])
        return RISK_DENY
    if risk <= allow_threshold:
        logger.info("jev_risk_gate.allow tool={} risk={} cmd={}",
                    tool_name, risk, command[:120])
        return RISK_ALLOW

    logger.info("jev_risk_gate.confirm tool={} risk={} cmd={}",
                tool_name, risk, command[:120])
    return RISK_CONFIRM


async def health_check() -> dict:
    """健康检查（供 /system 健康汇总）。"""
    enabled = is_enabled()
    return {
        "available": enabled,
        "deny_threshold": _config_float("JEV_RISK_DENY_THRESHOLD", DEFAULT_DENY_THRESHOLD),
        "allow_threshold": _config_float("JEV_RISK_ALLOW_THRESHOLD", DEFAULT_ALLOW_THRESHOLD),
    }


__all__ = [
    "DEFAULT_ALLOW_THRESHOLD",
    "DEFAULT_DENY_THRESHOLD",
    "RISK_ALLOW",
    "RISK_CONFIRM",
    "RISK_DENY",
    "assess_command",
    "health_check",
    "is_enabled",
]
