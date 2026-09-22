"""Jev 决策模型客户端（TypeSafe AI System One）。

定位说明（重要）：
    Jev **不是大语言模型**，不生成任何文字，也不走 OpenAI 兼容的
    ``/chat/completions``。它是 TypeSafe AI 的 System One 决策模型，
    接收一份 ``state`` 与一组带类型的 ``questions``，**并行返回带校准概率
    的类型化答案**。官方定位：``Unstructured state in, typed probabilistic
    decisions out``。

    因此本模块不注册为 LLM provider，也不参与 LLM 路由/降级链；它是供
    各调用点按需使用的「判断原语」，用来替代「用 LLM 生成文字再解析」
    的脆弱做法（典型痛点：字符串匹配失败、500ms 超时、token 浪费）。

HTTP 契约（https://docs.typesafe.ai/api）：
    POST {base_url}/v1/systemone
    Authorization: Bearer <key>
    Content-Type: application/json

    body = {
        "model": "jev-latest",
        "state": <str | dict | list>,
        "questions": {<你的key>: <Question>, ...},
    }

    三种 Question（原语）：
      - noul : {"type": "noul", "instructions": "...",
                "criteria": {"true": "...", "false": "..."}}      # 是/否
      - choice: {"type": "choice", "instructions": "...",
                 "criteria": {"optA": "说明", "optB": "说明"}}     # 多选一（≤255）
      - score : {"type": "score", "instructions": "...",
                 "criteria": ["低", "中", "高"]}                   # 打分（2~10 档）

    响应：
      {"model": "jev-1.13.0",
       "answers": {"<你的key>": {"type": ..., ...}},
       "usage": {"input_tokens": N, "output_tokens": M}}

    Answer 字段：
      - noul   : {"type":"noul","noul":0.95}                  # 0~1，无数值 confidence
      - choice : {"type":"choice","choice":"billing",
                  "probabilities":{...},"confidence":0.81}    # 概率和为 1
      - score  : {"type":"score","score":1.05,
                  "legend":{"0":"Calm",...},
                  "probabilities":{...},"confidence":0.92}

设计约束：
    - 所有 I/O 异常一律降级（返回 None / 默认值），绝不打断调用方主流程。
    - 问题 key 不发送给模型，仅用于本地索引；每条 instructions 必须自包含。
    - 同一次请求可并行问多个问题，加题几乎不增加响应时间 → 优先批量合并，
      不要把一个逻辑拆成多次调用（官方实测 13 题批量比逐个快 ~10 倍）。
    - 未配置 JEV_API_KEY 时：``available`` 为 False，所有调用立即返回 None，
      让调用方走原有降级路径（向后兼容，零行为变化）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from loguru import logger

from utils.http_pool import get_shared_client

# 默认模型：jev-latest 由服务端解析到具体版本（如 jev-1.13.0）
DEFAULT_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai"

# 默认超时：官方宣称 70~500ms，留足余量；超时即降级，不等太久
DEFAULT_TIMEOUT = 8.0


class JevError(RuntimeError):
    """Jev 调用失败（鉴权/限流/校验/网络）。调用方应捕获并降级。"""


class JevBudgetExceeded(JevError):
    """超过本次调用的问题数量预算（防误用批量接口造成意外开销）。"""


# 单次请求的问题数上限（防御性：官方未设硬上限，但过高会拉长输入）
MAX_QUESTIONS = 64


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def is_enabled() -> bool:
    """Jev 总开关是否打开（config.JEV_ENABLED / 环境变量 JEV_ENABLED）。

    默认关闭：保持历史行为（各接入点走原有规则/LLM 路径），
    避免升级后行为静默漂移。config 导入失败时降级读环境变量。
    """
    try:
        import config as _cfg
        return bool(getattr(_cfg, "JEV_ENABLED", False))
    except (ImportError, AttributeError, ValueError):
        return _env("JEV_ENABLED").lower() in ("1", "true", "yes", "on")


def is_available() -> bool:
    """Jev 是否可用 = 开关打开 且 已配置密钥。

    未启用/未配置时所有调用点应直接跳过，保持原有降级路径（零行为变化）。
    """
    return is_enabled() and bool(_env("JEV_API_KEY"))


def _base_url() -> str:
    return _env("JEV_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    return _env("JEV_API_KEY")


def _model() -> str:
    try:
        import config as _cfg
        return str(getattr(_cfg, "JEV_MODEL", "") or "") or _env("JEV_MODEL", DEFAULT_MODEL)
    except (ImportError, AttributeError, ValueError):
        return _env("JEV_MODEL", DEFAULT_MODEL)


def _default_timeout() -> float:
    """默认超时（config.JEV_TIMEOUT 优先，环境变量兜底）。"""
    try:
        import config as _cfg
        return float(getattr(_cfg, "JEV_TIMEOUT", DEFAULT_TIMEOUT))
    except (ImportError, AttributeError, ValueError, TypeError):
        return DEFAULT_TIMEOUT


# ── 问题构造器（原语 DSL）──────────────────────────────────────────


def noul(instructions: str, *, true_means: str = "", false_means: str = "") -> dict:
    """是/否问题（Noul）。返回答案中 `noul` 是「否」为 0、「是」为 1 的概率。

    instructions 必须是自包含的完整问题——问题 key 不会发送给模型。
    """
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true_means or false_means:
        criteria: dict[str, str] = {}
        if true_means:
            criteria["true"] = true_means
        if false_means:
            criteria["false"] = false_means
        q["criteria"] = criteria
    return q


def choice(instructions: str, options: dict[str, str]) -> dict:
    """多选一（Choice）。options 为 {选项值: 该选项的说明}，最多 255 项。

    选项说明（rubric）显著影响准确率；建议列表不覆盖全部输入时加上
    "other" / "none" 兜底项，避免模型被迫在错误选项间分配概率。
    """
    if not options:
        raise ValueError("choice 至少需要一个选项")
    if len(options) > 255:
        raise ValueError("choice 最多 255 个选项")
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, levels: list[str]) -> dict:
    """打分（Score）。levels 为有序档位描述，2~10 档。

    返回的 `score` 可能落在档位之间（如 1.05），适合阈值判断。
    """
    if len(levels) < 2 or len(levels) > 10:
        raise ValueError("score 档位数量需在 2~10 之间")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


# ── 核心调用 ──────────────────────────────────────────────────────


async def system_one(
    state: Any,
    questions: dict[str, dict],
    *,
    timeout: float | None = None,
) -> dict[str, dict] | None:
    """调用 /v1/systemone，返回 ``{question_key: answer_dict}``。

    失败（未启用 / 未配置 key / 网络 / 鉴权 / 限流 / 格式错误）一律返回 None，
    由调用方降级到原有逻辑。**不会抛异常**（除参数误用）。

    Args:
        state: 待评估内容。字符串、dict 或 list（聊天记录/记录/应用状态）。
        questions: {自定义key: noul()/choice()/score() 构造的 Question}。
        timeout: 请求超时（秒）；None 时取 config.JEV_TIMEOUT（默认 8s）。

    Returns:
        answers 映射；失败返回 None。
    """
    if not questions:
        return {}
    if len(questions) > MAX_QUESTIONS:
        raise JevBudgetExceeded(
            f"单次 system_one 问题数 {len(questions)} 超过上限 {MAX_QUESTIONS}"
        )

    # 总开关关闭时直接跳过（保持历史行为，不发起任何网络请求）
    if not is_enabled():
        logger.debug("jev.disabled")
        return None

    if timeout is None:
        timeout = _default_timeout()

    api_key = _api_key()
    if not api_key:
        logger.debug("jev.not_configured")
        return None

    payload = {"model": _model(), "state": state, "questions": questions}
    url = f"{_base_url()}/v1/systemone"

    try:
        client = get_shared_client()
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )
    except (httpx.TimeoutException, httpx.RequestError, OSError, RuntimeError) as e:
        # 超时/网络异常不属 OSError 全族（TimeoutException str 常为空），显式捕获
        logger.debug("jev.request_failed error={} type={}", repr(e)[:160], type(e).__name__)
        return None
    except Exception:
        logger.exception("jev.request_unexpected")
        return None

    if resp.status_code == 429 or resp.status_code == 529:
        # 官方建议指数退避；此处降级为上，交由调用方原路径兜底
        logger.warning("jev.rate_limited status={}", resp.status_code)
        return None
    if resp.status_code != 200:
        logger.warning("jev.http_error status={} body={}",
                       resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except (ValueError, TypeError) as e:
        logger.warning("jev.bad_json error={}", str(e))
        return None

    answers = data.get("answers")
    if not isinstance(answers, dict):
        logger.warning("jev.missing_answers keys={}", list(data.keys()))
        return None
    return answers


# ── 便捷读取（类型化答案 → 常用值）────────────────────────────────


def answer_noul(answers: dict[str, dict] | None, key: str) -> float | None:
    """取 Noul 概率（0~1）。缺失/类型不符返回 None。"""
    if not answers:
        return None
    item = answers.get(key)
    if not isinstance(item, dict) or item.get("type") != "noul":
        return None
    val = item.get("noul")
    return float(val) if isinstance(val, (int, float)) else None


def answer_choice(answers: dict[str, dict] | None, key: str) -> str | None:
    """取 Choice 选中项。缺失返回 None。"""
    if not answers:
        return None
    item = answers.get(key)
    if not isinstance(item, dict) or item.get("type") != "choice":
        return None
    val = item.get("choice")
    return str(val) if val is not None else None


def answer_choice_probs(answers: dict[str, dict] | None, key: str) -> dict[str, float]:
    """取 Choice 的完整概率分布（{选项: 概率}）。缺失返回空 dict。"""
    if not answers:
        return {}
    item = answers.get(key)
    if not isinstance(item, dict) or item.get("type") != "choice":
        return {}
    probs = item.get("probabilities")
    if not isinstance(probs, dict):
        return {}
    return {str(k): float(v) for k, v in probs.items() if isinstance(v, (int, float))}


def answer_score(answers: dict[str, dict] | None, key: str) -> float | None:
    """取 Score 分值（可能落在档位之间）。缺失返回 None。"""
    if not answers:
        return None
    item = answers.get(key)
    if not isinstance(item, dict) or item.get("type") != "score":
        return None
    val = item.get("score")
    return float(val) if isinstance(val, (int, float)) else None


def answer_confidence(answers: dict[str, dict] | None, key: str) -> float | None:
    """取 Choice/Score 的置信度（0~1）。Noul 无 confidence，返回 None。"""
    if not answers:
        return None
    item = answers.get(key)
    if not isinstance(item, dict):
        return None
    val = item.get("confidence")
    return float(val) if isinstance(val, (int, float)) else None


def confident_enough(answers: dict[str, dict] | None, key: str,
                     min_confidence: float) -> bool:
    """判断某个 Choice/Score 答案的置信度是否达到采纳门槛。

    官方 confidence-gated routing 模式：置信度只说明「模型有多确信」，
    不说明「答案对不对」。同一数值在不同场景代表不同风险，故阈值由调用方
    按该判断答错的后果严重程度传入。

    - min_confidence <= 0：完全信任（永不复核）
    - 答案缺失 / 无 confidence 字段：视为不够确信（交回调用方复核）
    - Noul 答案没有 confidence 字段：由调用方自行用概率做阈值判断
    """
    if min_confidence <= 0:
        return True
    conf = answer_confidence(answers, key)
    return conf is not None and conf >= min_confidence


async def probe(timeout: float = 8.0) -> tuple[bool, str]:
    """连通性探针：返回 (是否可用, 说明)。供向导/健康检查复用。"""
    if not is_available():
        return False, "未配置 JEV_API_KEY"
    answers = await system_one(
        state="probe",
        questions={"ok": noul("Is this a probe request?")},
        timeout=timeout,
    )
    if answers is None:
        return False, "Jev API 请求失败（网络/鉴权/限流）"
    if "ok" not in answers:
        return False, "Jev 返回缺少预期答案"
    return True, "Jev 决策模型连接正常"


async def health_check() -> dict:
    """健康检查（供 /system 健康汇总使用）。"""
    ok, message = await probe()
    return {
        "available": ok,
        "configured": is_available(),
        "model": _model(),
        "base_url": _base_url(),
        "message": message,
    }


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "MAX_QUESTIONS",
    "JevBudgetExceeded",
    "JevError",
    "answer_choice",
    "answer_choice_probs",
    "answer_confidence",
    "answer_noul",
    "answer_score",
    "choice",
    "confident_enough",
    "health_check",
    "is_available",
    "is_enabled",
    "noul",
    "probe",
    "score",
    "system_one",
]
