# core/intent_decomposition.py
"""
输出意图分解 — 对齐 SAELens 的稀疏自编码器范式。

SAE 将 d_model 维残差流编码为 d_sae 维稀疏特征:
    feature_acts = encode(x)     # [d_sae], 大部分为0
    x_recon = decode(feature_acts) # [d_model]

对应地，IntentDecomposition 将 Agent 输出编码为意图因子:
    factors = encode(output)       # 各意图的激活值
    reconstructed = decode(factors) # 重建输出(用于验证)

参考:
- SAELens/sae_lens/saes/sae.py: SAE.encode()/decode()
- SAELens/sae_lens/training/activations_store.py: ActivationsStore
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, ClassVar

from loguru import logger


@dataclass
class IntentFactor:
    """
    意图因子 — 对齐 SAELens/sae_lens/saes/sae.py 中 SAE 的稀疏特征。
    """
    name: str
    activation: float
    evidence: str = ""
    confidence: float = 1.0


@dataclass
class DecomposedOutput:
    """分解后的输出 — 对齐 SAE 的 encode 输出"""
    raw_output: str
    factors: list[IntentFactor]
    residual: float = 0.0
    total_dimensions: int = 0

    @property
    def dominant_intent(self) -> IntentFactor | None:
        """主导意图 — 激活最高的因子"""
        if not self.factors:
            return None
        return max(self.factors, key=lambda f: f.activation)

    @property
    def sparsity(self) -> float:
        """稀疏度 — 对齐 SAE 的 l0 稀疏度量"""
        if not self.factors:
            return 0.0
        total = self.total_dimensions or len(IntentDecomposer.INTENT_DIMENSIONS)
        active = sum(1 for f in self.factors if f.activation > 0.1)
        return 1.0 - active / max(total, 1)


class IntentDecomposer:
    """
    输出意图分解器 — 对齐 SAELens 的 SAE encode/decode 范式。
    """

    INTENT_DIMENSIONS: ClassVar[list[str]] = [
        "knowledge", "emotional", "safety", "creative",
        "factual", "social", "procedural",
    ]

    INTENT_KEYWORDS: ClassVar[dict[str, list[str]]] = {
        "knowledge": ["根据", "资料显示", "研究表明", "数据表明", "据统计",
                      "据了解", "据报道", "according to", "research shows"],
        "emotional": ["别担心", "加油", "理解你的感受", "心疼", "开心",
                      "难过", "陪伴", "安慰", "don't worry", "i understand"],
        "safety": ["请注意", "安全", "风险", "不建议", "谨慎",
                   "warning", "caution", "not recommended"],
        "creative": ["可以试试", "不如", "想象一下", "如果", "创意",
                     "how about", "what if", "imagine"],
        "factual": ["是", "位于", "成立于", "人口", "面积", "首都",
                    "is", "located", "founded"],
        "social": ["你好", "谢谢", "再见", "请问", "hello", "thank"],
        "procedural": ["步骤", "首先", "然后", "最后", "方法",
                       "step", "first", "then", "finally"],
    }

    LLM_TIMEOUT: ClassVar[float] = 10.0
    LLM_MAX_TOKENS: ClassVar[int] = 512
    LLM_TEMPERATURE: ClassVar[float] = 0.3

    _SYSTEM_PROMPT: ClassVar[str] = (
        "你是一个意图分析专家。分析文本中包含的意图成分，返回严格 JSON。\n\n"
        "可选意图维度：knowledge（知识引用）、emotional（情感回应）、"
        "safety（安全警示）、creative（创意建议）、factual（事实陈述）、"
        "social（社交寒暄）、procedural（步骤指导）。\n\n"
        "返回格式（严格 JSON，不要 markdown）：\n"
        '{"factors": [{"name": "意图名", "activation": 0.0~1.0, '
        '"evidence": "支持该意图的原文片段"}], "residual": 0.0~1.0}\n\n'
        "规则：\n"
        "- activation 表示该意图在文本中的强度，0=不存在，1=极强\n"
        "- residual 表示无法被任何意图解释的比例\n"
        "- 只列出 activation > 0.1 的意图\n"
        "- evidence 必须是原文中的实际片段"
    )

    # user 侧模板常量化：治理 override 可同时替换 system+user 双槽
    USER_ANALYZE_TEMPLATE: ClassVar[str] = "分析以下文本的意图成分：\n\n{text}"

    def __init__(self, use_llm_decomposition: bool = True):
        self._use_llm = use_llm_decomposition
        self._free_backend: Any = None
        # 当前功能节点后端（api/local/off），由 set_backend 维护；
        # 公开字段——路由层据此判断请求上限，禁止跨模块读写私有属性
        self.node_backend: str = "api"

    @property
    def use_llm(self) -> bool:
        """是否启用 LLM 分解。"""
        return self._use_llm

    def set_free_backend(self, backend: Any) -> None:
        """注入 FreeModelBackend，供 _llm_encode 调用硅基流动免费模型。"""
        self._free_backend = backend

    def set_backend(self, backend: str, local_model: str | None = None) -> None:
        """热切功能节点后端；off 使用确定性规则，不调用模型。"""
        normalized = "api" if backend == "auto" else backend
        if normalized not in {"local", "api", "off"}:
            raise ValueError(f"invalid intent decomposition backend: {backend}")
        self._use_llm = normalized != "off"
        self.node_backend = normalized
        if self._free_backend is None and normalized != "off":
            from utils.free_model_backend import FreeModelBackend

            self._free_backend = FreeModelBackend()
        if self._free_backend is not None:
            self._free_backend.set_backend(normalized, local_model)

    async def encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """将输出编码为意图因子 — 对齐 SAE.encode()"""
        if self._use_llm:
            return await self._llm_encode(output, context)
        return self._rule_encode(output, context)

    @staticmethod
    def _build_messages(output: str) -> list[dict]:
        """构建分析消息：production override 优先（system+user 双槽），缺省回退内置。"""
        try:
            from web.prompt_profile_repository import try_resolve

            override = try_resolve("intent.decompose", {"text": output})
        except Exception:
            override = None
        if override is not None:
            system_prompt, user_prompt = override
        else:
            system_prompt = IntentDecomposer._SYSTEM_PROMPT
            user_prompt = IntentDecomposer.USER_ANALYZE_TEMPLATE.replace(
                "{text}", output)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _rule_encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """规则基分解 — Phase 1 实现"""
        if not output:
            return DecomposedOutput(raw_output=output, factors=[], residual=1.0)

        factors = []
        text_lower = output.lower()

        for intent_name in self.INTENT_DIMENSIONS:
            keywords = self.INTENT_KEYWORDS.get(intent_name, [])
            score = self._score_keywords(text_lower, keywords)
            if score > 0:
                factors.append(IntentFactor(intent_name, score))

        # 不归一化 — 保留原始激活值
        # residual = 未被任何意图解释的比例 (对齐 SAE 的 reconstruction error)
        # 每个意图的 activation 上限为 1.0，总解释力 = sum(min(1, act))
        explained = sum(min(1.0, f.activation) for f in factors)
        residual = max(0.0, 1.0 - min(1.0, explained / len(self.INTENT_DIMENSIONS)))

        return DecomposedOutput(raw_output=output, factors=factors, residual=residual,
                                total_dimensions=len(self.INTENT_DIMENSIONS))

    def _score_keywords(self, text: str, keywords: list[str]) -> float:
        """简单的关键词匹配评分"""
        hits = sum(1 for kw in keywords if kw in text)
        if hits == 0:
            return 0.0
        return min(1.0, hits * 0.3)

    async def _llm_encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """LLM 基分解 — 通过硅基流动免费模型做结构化意图分析。

        调用链：FreeModelBackend.call() → 硅基流动 API / 本地模型。
        失败/超时/格式异常时静默 fallback 到 _rule_encode，不抛异常。
        """
        if not output:
            return DecomposedOutput(raw_output=output, factors=[], residual=1.0)

        backend = self._free_backend
        if backend is None:
            try:
                from utils.free_model_backend import FreeModelBackend
                backend = FreeModelBackend()
            except Exception as e:
                logger.debug("intent_decomposition.no_free_backend: {}", e)
                return self._rule_encode(output, context)

        if backend.disabled:
            logger.debug("intent_decomposition.backend_disabled")
            return self._rule_encode(output, context)

        # production override 优先（intent.decompose 治理 system+user 双槽），
        # 空白/异常回退内置；system 槽禁含未信任变量（渲染层强制）
        messages = self._build_messages(output)

        try:
            raw = await asyncio.wait_for(
                backend.call(
                    messages,
                    temperature=self.LLM_TEMPERATURE,
                    max_tokens=self.LLM_MAX_TOKENS,
                ),
                timeout=self.LLM_TIMEOUT,
            )
            if not raw:
                logger.debug("intent_decomposition.llm_empty_response")
                return self._rule_encode(output, context)
            return self._parse_llm_response(raw, output)
        except asyncio.TimeoutError:
            logger.debug("intent_decomposition.llm_timeout")
            return self._rule_encode(output, context)
        except Exception as e:
            logger.debug("intent_decomposition.llm_failed: {}", e)
            return self._rule_encode(output, context)

    def _parse_llm_response(self, raw: str, original_output: str) -> DecomposedOutput:
        """解析 LLM 返回的 JSON，构造 DecomposedOutput。解析失败 fallback 到规则编码。"""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1:]
            last_fence = cleaned.rfind("```")
            if last_fence != -1:
                cleaned = cleaned[:last_fence]
            cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.debug("intent_decomposition.json_parse_failed: raw={}", raw[:200])
            return self._rule_encode(original_output)

        factors_raw = data.get("factors", [])
        if not isinstance(factors_raw, list):
            return self._rule_encode(original_output)

        valid_names = set(self.INTENT_DIMENSIONS)
        factors: list[IntentFactor] = []
        for item in factors_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            if name not in valid_names:
                continue
            activation = float(item.get("activation", 0.0))
            activation = max(0.0, min(1.0, activation))
            if activation <= 0.1:
                continue
            evidence = str(item.get("evidence", ""))
            confidence = float(item.get("confidence", 1.0))
            confidence = max(0.0, min(1.0, confidence))
            factors.append(IntentFactor(name=name, activation=activation,
                                        evidence=evidence, confidence=confidence))

        residual = float(data.get("residual", 0.0))
        residual = max(0.0, min(1.0, residual))

        if not factors:
            logger.debug("intent_decomposition.llm_no_valid_factors")
            return self._rule_encode(original_output)

        return DecomposedOutput(
            raw_output=original_output,
            factors=factors,
            residual=residual,
            total_dimensions=len(self.INTENT_DIMENSIONS),
        )
