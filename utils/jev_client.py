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

⚠️ 能力边界（jev-1.13 能力探索报告 · 2026-09-23 实测 17 次调用，必须遵守）：
    放心交给它：意图路由/分类、风控门卫、相关性打分、是非判断（配逃生门）。
    要用但设防：中文（官方承认弱于英语）、模糊文本（会"温和偏自信"）、
                延迟（国内实测 0.7~1.1s，高于官方宣称的 70~500ms）。
    别指望它：数数（实测 0.81 置信答错）、算术、日期比较、多跳推理、生成文字。

    因此接入时必须做到：
      1. **永远留逃生门选项**（``ESCAPE_HATCH_KEY``）：不加时模型会"自信地乱选"
         （实测 0.80~0.85，与答对时几乎一样高）；加上「以上都不对」后立刻正确拒绝。
      2. **别只读 confidence**：概率是相对于**选项集合**的，不是相对于世界事实的。
         用 ``distribution_is_suspicious`` 看分布形状（摊薄/多峰 = 拿不准）。
         注意：支持度集中在单一选项是**理想情形**（实测答对常为 1.0），不是可疑。
      3. **计数/算术/日期/多跳逻辑留在代码里**，只把"答案空间有限"的语义判断给它。
      4. **state 只放问题需要的字段**：无关内容会稀释准确率（context rot）。
      5. **rubric 要具体**：选项说明应细到"两个审查者能达成一致"。
"""
from __future__ import annotations

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


# 逃生门选项的保留 key（调用方拿到此值时应视为「无法判断」，走升级路径）。
ESCAPE_HATCH_KEY = "__none__"


def escape_hatch_option(description: str = "") -> tuple[str, str]:
    """返回「以上都不对」逃生门选项的 ``(key, 说明)``，供调用方并入 options。

    实测依据（jev-1.13 能力探索报告 · 发现二）：
        选项都不匹配时，不加逃生门模型会「自信地乱选」（实测 0.80~0.85，
        与答对时的 0.9 几乎一样高，难以察觉）；加上「以上都不对」后
        立刻变成 1.0 正确拒绝。**这是最便宜的准确率提升。**

    用法::

        opts = {"a": "...", "b": "..."}
        opts[jev.ESCAPE_HATCH_KEY] = jev.escape_hatch_option("以上都不对")[1]
        # 拿到答案后：choice == ESCAPE_HATCH_KEY → 视为无法判断，升级复核
    """
    return ESCAPE_HATCH_KEY, (description or "以上都不对，多项都不合适或证据不足")


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

    # 预先校验可序列化：state 里混入 set/自定义对象等时，httpx 会在编码阶段
    # 抛 TypeError，混在网络异常里难以定位。这里提前检查并给出可读告警。
    try:
        import json as _json
        _json.dumps(payload)
    except (TypeError, ValueError) as e:
        logger.warning("jev.state_not_serializable error={}", str(e)[:200])
        return None

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
    except (ValueError, TypeError, KeyError, AttributeError, ImportError) as e:
        # 非网络类异常（响应结构畸形 / 客户端构建失败等）：同样降级，不打断调用方
        logger.warning("jev.request_unexpected error={} type={}",
                       repr(e)[:160], type(e).__name__)
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

    ⚠️ 官方与社区实测（jev-1.13）一致警告：**不要只读 confidence 数字**。
    「高置信 + 选项集合有偏」才是最危险的组合（模型会把支持度压给错误选项）。
    本函数只做数字门控，请配合 ``distribution_is_suspicious`` 一起判断。
    """
    if min_confidence <= 0:
        return True
    conf = answer_confidence(answers, key)
    return conf is not None and conf >= min_confidence


def distribution_is_suspicious(answers: dict[str, dict] | None, key: str,
                               *, min_top_prob: float = 0.5) -> tuple[bool, str]:
    """检查 Choice 概率分布的「形状」是否可疑——比单看 confidence 更能发现问题。

    实测依据（jev-1.13 能力探索报告 · 第五节「把握 ≠ 正确」）：
        模型概率是相对于**选项集合**的，不是相对于世界事实的。若选项集合里
        没有正确答案，它会把支持度压给某个错误选项，confidence 可能仍然很高
        （实测无逃生门时 0.80~0.85），此**时单看数字无法发现**，只能靠：
          1. 概率是否被"摊薄"到多个选项上（最高概率偏低 = 拿不准）
          2. 分布形状是「一峰独大」还是「多峰/平坦」

    ⚠️ 注意正向理解的坑：**支持度集中在单一选项是理想情形**（实测答对时
    常为 1.0，其余全 0），不是可疑信号。真正可疑的是**摊薄/多峰**。

    判定规则（任一命中即可疑）：
        - 最高概率 < min_top_prob（默认 0.5）：模型没有明确倾向
        - 非零选项（>1%）≥ 3 个：支持度被摊到 3 个以上，拿不准
    返回 ``(是否可疑, 原因)``。可疑 ≠ 一定错误，而是「值得让更强的模型复核」。

    Args:
        answers: system_one 的返回。
        key: 问题 id。
        min_top_prob: 最高概率下限；低于此值判可疑（默认 0.5，设为 0 关闭该规则）。
    """
    probs = answer_choice_probs(answers, key)
    if not probs:
        return True, "无概率分布"

    ordered = sorted(probs.values(), reverse=True)
    top = ordered[0]
    if min_top_prob > 0 and top < min_top_prob:
        return True, f"最高概率仅 {top:.2f}（< {min_top_prob}），模型没有明确倾向"

    # 非零选项数：>=4 个选项都有实体概率 = 严重摊薄（真的拿不准）。
    # 3 个非零在 5~6 选项的题里属常见（一个主选 + 两个小尾巴），不判可疑，
    # 由 min_top_prob 规则兜底（主选概率够高就说明倾向明确）。
    non_zero = [p for p in ordered if p > 0.01]
    if len(non_zero) >= 4:
        return True, f"支持度摊薄在 {len(non_zero)} 个选项上，模型拿不准"
    return False, ""


def answer_payload(answers: dict[str, dict] | None, key: str) -> dict:
    """返回原始 answer 结构（便于日志/审计时记录完整概率分布）。"""
    if not answers:
        return {}
    item = answers.get(key)
    return dict(item) if isinstance(item, dict) else {}


def evaluate_choice(answers: dict[str, dict] | None, key: str, *,
                    valid_options: Any = None,
                    min_confidence: float = 0.0,
                    check_distribution: bool = True) -> tuple[str | None, str]:
    """对 Choice 答案跑统一的三道防线，返回 ``(采纳值, 未采纳原因)``。

    抽出此函数是为了让各接入点（子代理路由 / 检索意图）**共用同一套门控规则**，
    避免逻辑在两处复制后漂移。三道防线（顺序固定，与实测报告一致）：

      1. **逃生门**：命中 ``ESCAPE_HATCH_KEY`` → 视为"无法判断"，交回升级
      2. **分布形状**：``distribution_is_suspicious`` 判为摊薄/无倾向 → 不可信
      3. **置信度阈值**：``confident_enough`` 不达标 → 交回复核

    Args:
        answers: ``system_one`` 的返回。
        key: 问题 id。
        valid_options: 允许的选项集合；None 表示不校验（逃生门已单独处理）。
        min_confidence: 置信度阈值（0 = 不启用该道防线）。
        check_distribution: 是否启用分布形状检查。

    Returns:
        ``(value, reason)``：采纳时 ``reason`` 为空串；未采纳时 ``value`` 为 None，
        ``reason`` 是简短的英文原因标签（供调用方拼日志，不直接展示给用户）。
    """
    value = answer_choice(answers, key)
    if not value:
        return None, "no_answer"
    if value == ESCAPE_HATCH_KEY:
        return None, "escape_hatch"
    if valid_options is not None and value not in valid_options:
        return None, "unknown_option"
    if check_distribution:
        suspicious, _why = distribution_is_suspicious(answers, key)
        if suspicious:
            return None, "suspicious_distribution"
    if not confident_enough(answers, key, min_confidence):
        return None, "low_confidence"
    return value, ""


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
    "answer_payload",
    "choice",
    "confident_enough",
    "distribution_is_suspicious",
    "escape_hatch_option",
    "evaluate_choice",
    "ESCAPE_HATCH_KEY",
    "health_check",
    "is_available",
    "is_enabled",
    "noul",
    "probe",
    "score",
    "system_one",
]
