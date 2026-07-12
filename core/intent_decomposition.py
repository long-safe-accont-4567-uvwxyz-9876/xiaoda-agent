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
from dataclasses import dataclass, field
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
        total = len(IntentDecomposer.INTENT_DIMENSIONS)
        active = sum(1 for f in self.factors if f.activation > 0.1)
        return 1.0 - active / total


class IntentDecomposer:
    """
    输出意图分解器 — 对齐 SAELens 的 SAE encode/decode 范式。
    """

    INTENT_DIMENSIONS = [
        "knowledge", "emotional", "safety", "creative",
        "factual", "social", "procedural",
    ]

    INTENT_KEYWORDS = {
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

    def __init__(self, use_llm_decomposition: bool = False):
        self._use_llm = use_llm_decomposition

    async def encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """将输出编码为意图因子 — 对齐 SAE.encode()"""
        if self._use_llm:
            return await self._llm_encode(output, context)
        return self._rule_encode(output, context)

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

        # 归一化
        if factors:
            total = sum(f.activation for f in factors)
            if total > 1.0:
                for f in factors:
                    f.activation /= total

        explained = sum(f.activation for f in factors)
        residual = max(0.0, 1.0 - explained)

        return DecomposedOutput(raw_output=output, factors=factors, residual=residual)

    def _score_keywords(self, text: str, keywords: list[str]) -> float:
        """简单的关键词匹配评分"""
        hits = sum(1 for kw in keywords if kw in text)
        if hits == 0:
            return 0.0
        return min(1.0, hits * 0.3)

    async def _llm_encode(self, output: str, context: dict | None = None) -> DecomposedOutput:
        """LLM 基分解 — Phase 2 实现（未实现）"""
        raise NotImplementedError("Phase 2: LLM-based decomposition")
