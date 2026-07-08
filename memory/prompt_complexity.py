"""提示词复杂度分析器 — 基于 Hecate (arXiv:2607.01903v1) 的 Prompt-as-Specification 模型。

Hecate 核心发现:
  1. 提示词是行为规范 (Hoare 逻辑的 NL 类比): 条件规则 + 全局不变量 + 状态谓词
  2. "结构广度" (structural breadth) 计数独立元素 > 原始体积 (size)
  3. 7 个通过 size 控制的指标: n_mem_refs(+0.40), n_llm_calls(+0.38),
     n_prompt_templates, n_conditional_rules(+0.27), n_invariants,
     n_tool_refs, n_state_preds
  4. 提示词复杂度是与代码复杂度独立的维度

本模块提供:
  - parse_prompt_spec(text): 解析提示词为 PromptSpec (规则/不变量/状态谓词)
  - count_structural_elements(source_dir, pattern): 静态计数代码层结构元素
  - score_prompt_complexity(prompt_text, source_dir): 综合复杂度评分
  - ComplexityReport: 完整复杂度报告, 含热点识别

设计原则:
  - 确定性: 纯静态分析, 不调用 LLM
  - 大小独立: 指标计数独立元素, 不跟踪体积
  - 双层覆盖: 同时分析 NL 提示词层和 Python 代码层
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


# ── NL 层: 提示词规范解析 ──────────────────────────────────────

# 条件规则模式 — Hecate Definition 1: condition + action + output constraint
# 中文条件触发词: 如果/当...时/若/一旦/假如/倘若/要是
# 英文条件触发词: if/when/whenever/unless/once/in case
_CONDITIONAL_PATTERNS = [
    re.compile(r"如果.+?[,，。；!！\n]", re.DOTALL),
    re.compile(r"当.+?时[,，。；!！\n]?", re.DOTALL),
    re.compile(r"若[^。！\n]+[，,。！\n]"),
    re.compile(r"一旦.+?[，,。！\n]"),
    re.compile(r"假如.+?[，,。！\n]"),
    re.compile(r"倘若.+?[，,。！\n]"),
    re.compile(r"要是.+?[，,。！\n]"),
    re.compile(r"\bif\b\s+.+?[,\n.]", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bwhen\b\s+.+?[,\n.]", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bwhenever\b\s+.+?[,\n.]", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bunless\b\s+.+?[,\n.]", re.IGNORECASE | re.DOTALL),
]

# 全局不变量模式 — Hecate: constraints that apply across all rules
# 中文: 必须/总是/绝不/禁止/不得/始终/一律
# 英文: must/always/never/shall/required/forbidden
_INVARIANT_PATTERNS = [
    re.compile(r"必须[^。！\n]*[。！\n]"),
    re.compile(r"不得[^。！\n]*[。！\n]"),
    re.compile(r"禁止[^。！\n]*[。！\n]"),
    re.compile(r"绝不[^。！\n]*[。！\n]"),
    re.compile(r"始终[^。！\n]*[。！\n]"),
    re.compile(r"一律[^。！\n]*[。！\n]"),
    re.compile(r"ⓘ\s*[^。！\n]*[。！\n]"),  # 规则标记
    re.compile(r"⚠️\s*[^。！\n]*[。！\n]"),  # 警告标记 (硬性规则)
    re.compile(r"\bmust\b\s+[^.\n]*[.\n]", re.IGNORECASE),
    re.compile(r"\balways\b\s+[^.\n]*[.\n]", re.IGNORECASE),
    re.compile(r"\bnever\b\s+[^.\n]*[.\n]", re.IGNORECASE),
    re.compile(r"\bshall\b\s+[^.\n]*[.\n]", re.IGNORECASE),
    re.compile(r"\bforbidden\b\s+[^.\n]*[.\n]", re.IGNORECASE),
]

# 状态谓词模式 — Hecate: context predicates the prompt conditions on
# 这些是提示词依赖的上下文状态, 不是规则也不是不变量
_STATE_PREDICATE_PATTERNS = [
    re.compile(r"\{address_term\}"),  # 用户称呼
    re.compile(r"\{user_id\}"),
    re.compile(r"\{session_id\}"),
    re.compile(r"\{emotion\}"),
    re.compile(r"\{xp_level\}"),
    re.compile(r"\{time\}"),
    re.compile(r"\{context\}"),
    re.compile(r"\[近期对话摘要\]"),
    re.compile(r"\[用户画像\]"),
    re.compile(r"\[心理状态\]"),
    re.compile(r"\[永久记忆\]"),
    re.compile(r"\[情感记忆\]"),
    re.compile(r"\[学习反馈\]"),
    re.compile(r"\[活跃约束\]"),
    re.compile(r"\[XP等级\]"),
    re.compile(r"\[情绪标签\]"),
    re.compile(r"\[场景约束\]"),
]


@dataclass
class PromptSpec:
    """Prompt-as-Specification 解析结果 (Hecate Definition 1).

    提示词被建模为三部分:
      - behavioral_rules: 行为规则 (条件 + 动作 + 输出约束)
      - global_invariants: 全局不变量 (跨所有规则的约束)
      - state_predicates: 状态谓词 (提示词依赖的上下文)
    """
    behavioral_rules: list[str] = field(default_factory=list)
    global_invariants: list[str] = field(default_factory=list)
    state_predicates: list[str] = field(default_factory=list)

    @property
    def n_conditional_rules(self) -> int:
        """条件规则数 (Hecate 指标: ρ=+0.27 in prompt vs +0.06 in code)."""
        return len(self.behavioral_rules)

    @property
    def n_invariants(self) -> int:
        """全局不变量数."""
        return len(self.global_invariants)

    @property
    def n_state_preds(self) -> int:
        """状态谓词数."""
        return len(self.state_predicates)

    def to_dict(self) -> dict:
        return {
            "behavioral_rules": self.behavioral_rules,
            "global_invariants": self.global_invariants,
            "state_predicates": self.state_predicates,
            "n_conditional_rules": self.n_conditional_rules,
            "n_invariants": self.n_invariants,
            "n_state_preds": self.n_state_preds,
        }


def parse_prompt_spec(text: str) -> PromptSpec:
    """将提示词文本解析为 PromptSpec.

    使用正则模式提取:
      - 条件规则: "如果...就", "当...时", "if...then", "when..."
      - 全局不变量: "必须", "不得", "禁止", "must", "always", "never"
      - 状态谓词: {占位符}, [段落标记]

    Args:
        text: 提示词文本 (system prompt, personality md, inline prompt template)

    Returns:
        PromptSpec 实例
    """
    spec = PromptSpec()

    # 提取条件规则 (去重)
    seen_rules: set[str] = set()
    for pattern in _CONDITIONAL_PATTERNS:
        for match in pattern.finditer(text):
            rule = match.group().strip().rstrip(",，。；!！\n")
            if rule and rule not in seen_rules and len(rule) > 3:
                spec.behavioral_rules.append(rule)
                seen_rules.add(rule)

    # 提取全局不变量 (去重)
    seen_inv: set[str] = set()
    for pattern in _INVARIANT_PATTERNS:
        for match in pattern.finditer(text):
            inv = match.group().strip().rstrip("。！\n")
            if inv and inv not in seen_inv and len(inv) > 3:
                spec.global_invariants.append(inv)
                seen_inv.add(inv)

    # 提取状态谓词 (去重)
    seen_pred: set[str] = set()
    for pattern in _STATE_PREDICATE_PATTERNS:
        for match in pattern.finditer(text):
            pred = match.group()
            if pred not in seen_pred:
                spec.state_predicates.append(pred)
                seen_pred.add(pred)

    return spec


# ── 代码层: 结构元素计数 ───────────────────────────────────────

# LLM 调用模式 — Hecate 指标 n_llm_calls (ρ=+0.38, 第二强)
_LLM_CALL_PATTERNS = [
    re.compile(r"\.route\s*\("),
    re.compile(r"\.chat_stream\s*\("),
    re.compile(r"\.chat\.completions\.create\s*\("),
    re.compile(r"router\.route\s*\("),
    re.compile(r"router\.chat_stream\s*\("),
    re.compile(r"router\._client\.chat\.completions\.create\s*\("),
]

# 记忆引用模式 — Hecate 指标 n_mem_refs (ρ=+0.40, 最强)
_MEMORY_REF_PATTERNS = [
    re.compile(r"\.retrieve_memories\s*\("),
    re.compile(r"\.retrieve_memories_hybrid\s*\("),
    re.compile(r"\.encode_memory\s*\("),
    re.compile(r"\.retrieve_comfort_memories\s*\("),
    re.compile(r"\.recall\s*\("),
    re.compile(r"\.recall_and_enact\s*\("),
    re.compile(r"\.restore_from_db\s*\("),
    re.compile(r"\.run_scheduled_recall\s*\("),
    re.compile(r"memory\.retrieve"),
    re.compile(r"memory\.encode"),
    re.compile(r"em_mgr\.recall"),
    re.compile(r"context\.restore"),
]

# 工具引用模式 — Hecate 指标 n_tool_refs
_TOOL_REF_PATTERNS = [
    re.compile(r"register_tool\s*\("),
    re.compile(r"register_lazy_tool\s*\("),
    re.compile(r"register_tool_direct\s*\("),
    re.compile(r"to_openai_tools\s*\("),
    re.compile(r"list_tools\s*\("),
    re.compile(r"@register_tool"),
    re.compile(r"\bimport\b.*\b(register_tool|to_openai_tools|list_tools)\b"),
]

# 提示词模板模式 — Hecate 指标 n_prompt_templates
_PROMPT_TEMPLATE_PATTERNS = [
    re.compile(r"([A-Z_]+_PROMPT)\s*=\s*[\"'`]"),
    re.compile(r"build_system_prompt\s*\("),
    re.compile(r"build_safe_system_prompt\s*\("),
    re.compile(r"build_scene_aware_prompt\s*\("),
    re.compile(r"build_instinct_prompt\s*\("),
    re.compile(r"_build_stable_prompt\s*\("),
    re.compile(r"_build_dynamic_prompt\s*\("),
    re.compile(r"_inject_dynamic_segments\s*\("),
    re.compile(r"_inject_xp_and_extra\s*\("),
    re.compile(r"_build_xp_segment\s*\("),
]


@dataclass
class StructuralCounts:
    """代码层结构元素计数 (Hecate 结构广度指标)."""
    n_llm_calls: int = 0       # LLM 调用站点数 (ρ=+0.38)
    n_mem_refs: int = 0        # 记忆引用数 (ρ=+0.40, 最强)
    n_tool_refs: int = 0       # 工具引用数
    n_prompt_templates: int = 0  # 提示词模板数

    # 调用位置详情 (用于热点识别)
    llm_call_sites: list[str] = field(default_factory=list)
    mem_ref_sites: list[str] = field(default_factory=list)
    tool_ref_sites: list[str] = field(default_factory=list)
    prompt_template_sites: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """结构广度总和."""
        return self.n_llm_calls + self.n_mem_refs + self.n_tool_refs + self.n_prompt_templates

    def to_dict(self) -> dict:
        return {
            "n_llm_calls": self.n_llm_calls,
            "n_mem_refs": self.n_mem_refs,
            "n_tool_refs": self.n_tool_refs,
            "n_prompt_templates": self.n_prompt_templates,
            "total": self.total,
            "llm_call_sites": self.llm_call_sites,
            "mem_ref_sites": self.mem_ref_sites,
            "tool_ref_sites": self.tool_ref_sites,
            "prompt_template_sites": self.prompt_template_sites,
        }


def _count_pattern_in_file(filepath: Path, patterns: list[re.Pattern]) -> list[str]:
    """在单个文件中计数模式匹配, 返回匹配行列表."""
    try:
        text = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        logger.debug("prompt_complexity.count_pattern_file_read_error: {}", exc_info=True)
        return []

    matches: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        # 跳过注释行
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for pattern in patterns:
            if pattern.search(line):
                matches.append(f"{filepath.name}:{line_no}: {line.strip()[:80]}")
                break  # 每行只计一次
    return matches


def count_structural_elements(source_dir: Path | str,
                               file_pattern: str = "*.py",
                               ) -> StructuralCounts:
    """静态计数代码层结构元素 (Hecate 结构广度指标).

    扫描 source_dir 下所有匹配 file_pattern 的 Python 文件,
    计数 LLM 调用/记忆引用/工具引用/提示词模板.

    Args:
        source_dir: 源代码目录
        file_pattern: 文件匹配模式 (默认 *.py)

    Returns:
        StructuralCounts 实例, 含每个类别的计数和调用位置
    """
    source_path = Path(source_dir)
    counts = StructuralCounts()

    if not source_path.exists():
        logger.warning("prompt_complexity.source_dir_not_found", dir=str(source_path))
        return counts

    # 收集所有 Python 文件 (排除 tests, __pycache__, .git)
    py_files: list[Path] = []
    for p in source_path.rglob(file_pattern):
        parts = p.parts
        if any(skip in parts for skip in ("__pycache__", ".git", "node_modules", ".venv")):
            continue
        py_files.append(p)

    for filepath in py_files:
        # LLM 调用
        llm_hits = _count_pattern_in_file(filepath, _LLM_CALL_PATTERNS)
        counts.llm_call_sites.extend(llm_hits)
        counts.n_llm_calls += len(llm_hits)

        # 记忆引用
        mem_hits = _count_pattern_in_file(filepath, _MEMORY_REF_PATTERNS)
        counts.mem_ref_sites.extend(mem_hits)
        counts.n_mem_refs += len(mem_hits)

        # 工具引用
        tool_hits = _count_pattern_in_file(filepath, _TOOL_REF_PATTERNS)
        counts.tool_ref_sites.extend(tool_hits)
        counts.n_tool_refs += len(tool_hits)

        # 提示词模板
        tpl_hits = _count_pattern_in_file(filepath, _PROMPT_TEMPLATE_PATTERNS)
        counts.prompt_template_sites.extend(tpl_hits)
        counts.n_prompt_templates += len(tpl_hits)

    return counts


# ── 综合复杂度评分 ──────────────────────────────────────────────

@dataclass
class PromptComplexityScore:
    """提示词复杂度评分 (Hecate 启发).

    结构广度指标 (size-independent):
      - n_conditional_rules: 条件规则数 (ρ=+0.27)
      - n_invariants: 全局不变量数
      - n_state_preds: 状态谓词数
      - n_llm_calls: LLM 调用站点数 (ρ=+0.38)
      - n_mem_refs: 记忆引用数 (ρ=+0.40, 最强)
      - n_tool_refs: 工具引用数
      - n_prompt_templates: 提示词模板数

    综合分 = 加权和 (权重来自 Hecate 论文的相关系数)
    """
    n_conditional_rules: int = 0
    n_invariants: int = 0
    n_state_preds: int = 0
    n_llm_calls: int = 0
    n_mem_refs: int = 0
    n_tool_refs: int = 0
    n_prompt_templates: int = 0

    # 原始大小 (用于验证 size 独立性)
    prompt_loc: int = 0  # 提示词行数
    code_loc: int = 0    # 代码行数

    @property
    def structural_breadth(self) -> int:
        """结构广度总和 (Hecate 核心概念: 计数独立元素, 非体积)."""
        return (self.n_conditional_rules + self.n_invariants + self.n_state_preds +
                self.n_llm_calls + self.n_mem_refs + self.n_tool_refs + self.n_prompt_templates)

    @property
    def complexity_score(self) -> float:
        """加权复杂度分 (权重来自 Hecate 论文相关系数).

        n_mem_refs: 0.40 (最强)
        n_llm_calls: 0.38
        n_conditional_rules: 0.27 (prompt 层, vs code 层 0.06)
        n_invariants: 0.22 (估计值, 论文未单独报告)
        n_prompt_templates: 0.25 (估计值)
        n_tool_refs: 0.20 (估计值)
        n_state_preds: 0.15 (估计值)
        """
        return (
            self.n_mem_refs * 0.40 +
            self.n_llm_calls * 0.38 +
            self.n_conditional_rules * 0.27 +
            self.n_prompt_templates * 0.25 +
            self.n_invariants * 0.22 +
            self.n_tool_refs * 0.20 +
            self.n_state_preds * 0.15
        )

    @property
    def is_high_complexity(self) -> bool:
        """是否为高复杂度 (超过预算阈值).

        阈值基于 Hecate 论文中 top-2 指标的效应量:
        n_mem_refs(+0.40) 和 n_llm_calls(+0.38) 的加权和 >= 5.0
        """
        return self.complexity_score >= 5.0

    def to_dict(self) -> dict:
        return {
            "n_conditional_rules": self.n_conditional_rules,
            "n_invariants": self.n_invariants,
            "n_state_preds": self.n_state_preds,
            "n_llm_calls": self.n_llm_calls,
            "n_mem_refs": self.n_mem_refs,
            "n_tool_refs": self.n_tool_refs,
            "n_prompt_templates": self.n_prompt_templates,
            "structural_breadth": self.structural_breadth,
            "complexity_score": round(self.complexity_score, 3),
            "is_high_complexity": self.is_high_complexity,
            "prompt_loc": self.prompt_loc,
            "code_loc": self.code_loc,
        }


def score_prompt_complexity(prompt_text: str,
                             source_dir: Path | str | None = None,
                             ) -> PromptComplexityScore:
    """综合评分提示词复杂度.

    Args:
        prompt_text: 提示词文本 (system prompt, personality md, 等)
        source_dir: 源代码目录 (如提供, 同时计数代码层结构元素)

    Returns:
        PromptComplexityScore 实例
    """
    # NL 层: 解析提示词规范
    spec = parse_prompt_spec(prompt_text)
    prompt_loc = len(prompt_text.splitlines())

    score = PromptComplexityScore(
        n_conditional_rules=spec.n_conditional_rules,
        n_invariants=spec.n_invariants,
        n_state_preds=spec.n_state_preds,
        prompt_loc=prompt_loc,
    )

    # 代码层: 计数结构元素
    if source_dir is not None:
        counts = count_structural_elements(source_dir)
        score.n_llm_calls = counts.n_llm_calls
        score.n_mem_refs = counts.n_mem_refs
        score.n_tool_refs = counts.n_tool_refs
        score.n_prompt_templates = counts.n_prompt_templates
        # 代码 LOC (近似)
        source_path = Path(source_dir)
        code_loc = 0
        for p in source_path.rglob("*.py"):
            if any(s in p.parts for s in ("__pycache__", ".git", ".venv")):
                continue
            try:
                code_loc += len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            except Exception:
                logger.debug("prompt_complexity.code_loc_count_error: {}", exc_info=True)
        score.code_loc = code_loc

    return score


# ── 复杂度报告 + 热点识别 ───────────────────────────────────────

@dataclass
class ComponentComplexity:
    """单个组件的复杂度信息."""
    name: str
    filepath: str
    score: PromptComplexityScore
    spec: PromptSpec | None = None
    counts: StructuralCounts | None = None


@dataclass
class ComplexityReport:
    """完整复杂度报告 (Hecate 启发).

    包含:
      - 总体复杂度评分
      - 各组件复杂度排名 (热点识别)
      - NL 层 vs 代码层对比
      - size 独立性验证数据
    """
    total_score: PromptComplexityScore
    components: list[ComponentComplexity] = field(default_factory=list)
    hotspots: list[ComponentComplexity] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_score": self.total_score.to_dict(),
            "components": [
                {
                    "name": c.name,
                    "filepath": c.filepath,
                    "score": c.score.to_dict(),
                    "spec": c.spec.to_dict() if c.spec else None,
                    "counts": c.counts.to_dict() if c.counts else None,
                }
                for c in self.components
            ],
            "hotspots": [
                {
                    "name": c.name,
                    "filepath": c.filepath,
                    "complexity_score": c.score.complexity_score,
                    "structural_breadth": c.score.structural_breadth,
                }
                for c in self.hotspots
            ],
        }

    def summary(self) -> str:
        """生成可读的复杂度报告摘要."""
        lines = [
            "=" * 60,
            "  提示词复杂度报告 (Hecate 启发)",
            "=" * 60,
            "",
            "【总体复杂度】",
            f"  结构广度总和:     {self.total_score.structural_breadth}",
            f"  加权复杂度分:     {self.total_score.complexity_score:.3f}",
            f"  高复杂度:         {'是' if self.total_score.is_high_complexity else '否'}",
            f"  提示词行数:       {self.total_score.prompt_loc}",
            f"  代码行数:         {self.total_score.code_loc}",
            "",
            "【结构广度分解】",
            f"  n_mem_refs        {self.total_score.n_mem_refs:>4d}  (ρ=+0.40, 最强)",
            f"  n_llm_calls       {self.total_score.n_llm_calls:>4d}  (ρ=+0.38)",
            f"  n_cond_rules      {self.total_score.n_conditional_rules:>4d}  (ρ=+0.27, prompt层)",
            f"  n_prompt_tpls     {self.total_score.n_prompt_templates:>4d}",
            f"  n_invariants      {self.total_score.n_invariants:>4d}",
            f"  n_tool_refs       {self.total_score.n_tool_refs:>4d}",
            f"  n_state_preds     {self.total_score.n_state_preds:>4d}",
            "",
        ]

        if self.hotspots:
            lines.append("【复杂度热点 (Top 5)】")
            for i, c in enumerate(self.hotspots[:5], 1):
                lines.append(
                    f"  {i}. {c.name} "
                    f"(score={c.score.complexity_score:.2f}, "
                    f"breadth={c.score.structural_breadth})"
                )
                lines.append(f"     {c.filepath}")
            lines.append("")

        # size 独立性验证
        if self.total_score.code_loc > 0:
            breadth_per_loc = self.total_score.structural_breadth / max(self.total_score.code_loc, 1)
            lines.append("【大小独立性验证】")
            lines.append(f"  结构广度/代码LOC:  {breadth_per_loc:.4f}")
            lines.append(f"  复杂度分/代码LOC:  {self.total_score.complexity_score / max(self.total_score.code_loc, 1):.6f}")
            lines.append("  → 指标计数独立元素, 非体积代理 (Hecate: size控制后仍显著)")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


def analyze_prompt_components(source_dir: Path | str,
                               prompt_files: list[Path | str] | None = None,
                               ) -> ComplexityReport:
    """分析提示词组件复杂度, 生成完整报告.

    Args:
        source_dir: 源代码目录
        prompt_files: 提示词文件列表 (如不提供, 自动扫描 config/workspace/*.md 和 config/agents/*.md)

    Returns:
        ComplexityReport 实例
    """
    source_path = Path(source_dir)

    # 1. 总体代码层计数
    total_counts = count_structural_elements(source_path)
    total_score = PromptComplexityScore(
        n_llm_calls=total_counts.n_llm_calls,
        n_mem_refs=total_counts.n_mem_refs,
        n_tool_refs=total_counts.n_tool_refs,
        n_prompt_templates=total_counts.n_prompt_templates,
        code_loc=sum(
            len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            for p in source_path.rglob("*.py")
            if not any(s in p.parts for s in ("__pycache__", ".git", ".venv"))
        ),
    )

    # 2. 扫描提示词文件
    if prompt_files is None:
        prompt_files = []
        # 工作区模板
        workspace_dir = source_path / "config" / "workspace"
        if workspace_dir.exists():
            prompt_files.extend(sorted(workspace_dir.glob("*.md")))
            prompt_files.extend(sorted(workspace_dir.glob("*.tpl")))
        # 子 Agent 人格
        agents_dir = source_path / "config" / "agents"
        if agents_dir.exists():
            prompt_files.extend(sorted(agents_dir.glob("*_personality.md")))

    components: list[ComponentComplexity] = []
    for pf in prompt_files:
        pf_path = Path(pf)
        if not pf_path.exists():
            continue
        try:
            text = pf_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            logger.debug("prompt_complexity.component_file_read_error: {}", exc_info=True)
            continue
        spec = parse_prompt_spec(text)
        prompt_loc = len(text.splitlines())
        score = PromptComplexityScore(
            n_conditional_rules=spec.n_conditional_rules,
            n_invariants=spec.n_invariants,
            n_state_preds=spec.n_state_preds,
            prompt_loc=prompt_loc,
        )
        components.append(ComponentComplexity(
            name=pf_path.stem,
            filepath=str(pf_path),
            score=score,
            spec=spec,
        ))
        # 累加到总分
        total_score.n_conditional_rules += spec.n_conditional_rules
        total_score.n_invariants += spec.n_invariants
        total_score.n_state_preds += spec.n_state_preds
        total_score.prompt_loc += prompt_loc

    # 3. 热点识别 (按复杂度分排序)
    hotspots = sorted(components, key=lambda c: c.score.complexity_score, reverse=True)

    return ComplexityReport(
        total_score=total_score,
        components=components,
        hotspots=hotspots,
    )


# ── 提示词版本追踪 (C2: 复杂度治理) ─────────────────────────────

def compute_prompt_hash(prompt_text: str) -> str:
    """计算提示词文本的 SHA-256 哈希 (用于版本追踪)."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def check_complexity_budget(prompt_text: str,
                             source_dir: Path | str | None = None,
                             max_complexity: float = 10.0,
                             max_conditional_rules: int = 30,
                             ) -> dict:
    """检查提示词复杂度是否超出预算.

    Args:
        prompt_text: 提示词文本
        source_dir: 源代码目录
        max_complexity: 最大允许复杂度分
        max_conditional_rules: 最大允许条件规则数

    Returns:
        {
            "within_budget": bool,
            "score": float,
            "violations": list[str],
            "hash": str,
        }
    """
    score = score_prompt_complexity(prompt_text, source_dir)
    violations: list[str] = []

    if score.complexity_score > max_complexity:
        violations.append(
            f"复杂度分 {score.complexity_score:.2f} 超出预算 {max_complexity}"
        )
    if score.n_conditional_rules > max_conditional_rules:
        violations.append(
            f"条件规则数 {score.n_conditional_rules} 超出上限 {max_conditional_rules}"
        )
    if score.is_high_complexity and score.complexity_score > max_complexity:
        violations.append("标记为高复杂度, 建议拆分或模块化")

    return {
        "within_budget": len(violations) == 0,
        "score": round(score.complexity_score, 3),
        "structural_breadth": score.structural_breadth,
        "violations": violations,
        "hash": compute_prompt_hash(prompt_text),
        "metrics": score.to_dict(),
    }


# ── 场景排序 × 复杂度对齐分析 (整合 prompt_builder 场景感知排序) ──
#
# 整合点:
#   prompt_builder.py 的 _MODULE_SCENE_PRIORITY 矩阵决定每个场景下模块的排序
#   prompt_complexity.py 的 parse_prompt_spec 计算每个模块的结构广度
#
#   两者已对齐 (v0.4.95): 桶排序按矩阵均值推导, 7/10完美 + 2/10微偏 + 1/10中偏(debug),
#   完全没有考虑模块的复杂度。Hecate 核心发现: 结构广度 > 体积,
#   高复杂度模块在非关键场景下的排序策略应该不同。
#
#   本节提供:
#     1. compute_module_complexity_map() — 预计算每个模块的复杂度
#     2. analyze_scene_complexity_alignment() — 分析排序与复杂度的对齐度
#     3. recommend_priority_adjustment() — 推荐优先级矩阵调整


@dataclass
class Inversion:
    """复杂度倒挂结构化记录 (用于推荐优先级调整)."""
    high_complexity_module: str  # 高复杂度但低优先级的模块 (应被提升)
    low_complexity_module: str   # 低复杂度但高优先级的模块
    high_priority: float         # 高复杂度模块当前的优先级
    low_priority: float          # 低复杂度模块当前的优先级
    high_complexity: float       # 高复杂度模块的复杂度
    low_complexity: float        # 低复杂度模块的复杂度

    def __str__(self) -> str:
        return (
            f"{self.high_complexity_module}(复杂度{self.high_complexity:.2f}, "
            f"优先级{self.high_priority:.0f}) 排在 "
            f"{self.low_complexity_module}(复杂度{self.low_complexity:.2f}, "
            f"优先级{self.low_priority:.0f}) 前面 — 高复杂度模块远离用户输入"
        )


@dataclass
class Concentration:
    """复杂度集中结构化记录."""
    position: int                # 起始位置索引
    modules: list[str]           # 聚集的模块名
    avg_complexity: float        # 平均复杂度

    def __str__(self) -> str:
        return (
            f"位置 {self.position}-{self.position + len(self.modules) - 1}: "
            f"{' + '.join(self.modules)} 复杂度均高于平均({self.avg_complexity:.2f})"
        )


@dataclass
class Mismatch:
    """场景不匹配结构化记录."""
    scene: str
    key_module: str              # 场景关键模块 (最高优先级)
    key_complexity: float        # 关键模块复杂度
    avg_complexity: float        # 平均复杂度

    def __str__(self) -> str:
        return (
            f"场景 '{self.scene}' 的关键模块 {self.key_module} "
            f"(复杂度{self.key_complexity:.2f}) 远低于平均({self.avg_complexity:.2f}) "
            f"— 场景目标与 prompt 内容可能错位"
        )


@dataclass
class SceneComplexityAlignment:
    """单个场景的复杂度对齐分析结果."""
    scene: str
    # 当前排序: [(module_name, priority_score, complexity_score), ...] 按优先级升序
    ordering: list[tuple[str, float, float]] = field(default_factory=list)
    # 复杂度倒挂: 高复杂度模块在低优先级位置 (远离用户输入)
    inversions: list[Inversion] = field(default_factory=list)
    # 复杂度集中: 多个高复杂度模块聚集在一起
    concentrations: list[Concentration] = field(default_factory=list)
    # 场景不匹配: 场景关键模块复杂度低, 无关高复杂度模块更靠近用户
    mismatches: list[Mismatch] = field(default_factory=list)
    # 场景复杂度总分 (所有模块复杂度加权和, 权重=优先级归一化)
    weighted_complexity: float = 0.0

    @property
    def is_aligned(self) -> bool:
        """排序与复杂度是否对齐 (无倒挂/集中/不匹配)."""
        return not self.inversions and not self.concentrations and not self.mismatches

    def to_dict(self) -> dict:
        return {
            "scene": self.scene,
            "is_aligned": self.is_aligned,
            "ordering": [
                {"module": m, "priority": p, "complexity": c}
                for m, p, c in self.ordering
            ],
            "inversions": [str(i) for i in self.inversions],
            "concentrations": [str(c) for c in self.concentrations],
            "mismatches": [str(m) for m in self.mismatches],
            "weighted_complexity": round(self.weighted_complexity, 3),
        }


def compute_module_complexity_map(source_dir: Path | str) -> dict[str, float]:
    """预计算每个提示词模块的复杂度分数.

    扫描 config/workspace/*.md 和 config/agents/*_personality.md,
    返回 {模块名: 复杂度分}.

    Args:
        source_dir: 项目根目录

    Returns:
        {模块名: 复杂度分} 字典, 模块名与 _MODULE_SCENE_PRIORITY 的 key 对齐
    """
    source_path = Path(source_dir)
    workspace_dir = source_path / "config" / "workspace"
    complexity_map: dict[str, float] = {}

    # 映射 _MODULE_SCENE_PRIORITY 的 key 到实际文件 (9 个模块)
    module_files = {
        "AGENTS.md": workspace_dir / "AGENTS.md",
        "SOUL.md": workspace_dir / "SOUL.md.tpl",  # 模板文件
        "IDENTITY.md": workspace_dir / "IDENTITY.md.tpl",
        "TOOLS.md": workspace_dir / "TOOLS.md",
        "USER.md": workspace_dir / "USER.md.tpl",
        "MEMORY.md": workspace_dir / "MEMORY.md.tpl",
        "HEARTBEAT.md": workspace_dir / "HEARTBEAT.md",
    }

    for module_name, filepath in module_files.items():
        if not filepath.exists():
            # 尝试不带 .tpl 后缀
            alt = filepath.with_suffix("")
            if alt.exists():
                filepath = alt
            else:
                complexity_map[module_name] = 0.0
                continue
        try:
            text = filepath.read_text(encoding="utf-8", errors="ignore")
            spec = parse_prompt_spec(text)
            score = PromptComplexityScore(
                n_conditional_rules=spec.n_conditional_rules,
                n_invariants=spec.n_invariants,
                n_state_preds=spec.n_state_preds,
            )
            complexity_map[module_name] = score.complexity_score
        except Exception:
            logger.debug("prompt_complexity.module_complexity_error: {}", exc_info=True)
            complexity_map[module_name] = 0.0

    # skills 和 hardware 是动态内容, 复杂度设为低值
    complexity_map.setdefault("skills", 0.5)
    complexity_map.setdefault("hardware", 0.3)

    return complexity_map


def analyze_scene_complexity_alignment(
        source_dir: Path | str,
        priority_matrix: dict[str, dict[str, int]] | None = None,
        complexity_map: dict[str, float] | None = None,
        inversion_threshold: float = 1.0,
        ) -> list[SceneComplexityAlignment]:
    """分析场景排序与模块复杂度的对齐度.

    连接 prompt_builder._MODULE_SCENE_PRIORITY (排序) 和
    prompt_complexity (复杂度), 识别:

      1. 复杂度倒挂: 高复杂度模块在低优先级位置 (远离用户输入)
         → 不变量/条件规则离用户太远, 可能被 LLM 忽略
      2. 复杂度集中: 多个高复杂度模块聚集在相邻位置
         → prompt 局部过载, LLM 注意力分散
      3. 场景不匹配: 场景关键模块复杂度低, 无关高复杂度模块更靠近用户
         → 场景目标与 prompt 内容错位

    Args:
        source_dir: 项目根目录
        priority_matrix: 优先级矩阵 (默认从 prompt_builder 导入)
        complexity_map: 模块复杂度 (默认实时计算)
        inversion_threshold: 倒挂检测阈值 (复杂度差 > 此值视为倒挂)

    Returns:
        每个场景的 SceneComplexityAlignment 列表
    """
    # 获取优先级矩阵
    if priority_matrix is None:
        try:
            import prompt_builder
            priority_matrix = prompt_builder._MODULE_SCENE_PRIORITY
        except (ImportError, AttributeError):
            # 回退: 使用默认矩阵
            priority_matrix = {
                "AGENTS.md":   {"default": 5, "greeting": 2, "task": 8, "emotional": 3, "identity": 4, "tool": 7},
                "SOUL.md":     {"default": 6, "greeting": 10, "task": 4, "emotional": 10, "identity": 8, "tool": 3},
                "IDENTITY.md": {"default": 4, "greeting": 3, "task": 3, "emotional": 4, "identity": 10, "tool": 2},
                "TOOLS.md":    {"default": 3, "greeting": 1, "task": 7, "emotional": 1, "identity": 2, "tool": 10},
                "skills":      {"default": 2, "greeting": 1, "task": 6, "emotional": 1, "identity": 1, "tool": 8},
                "hardware":    {"default": 1, "greeting": 1, "task": 5, "emotional": 1, "identity": 1, "tool": 6},
            }

    # 获取复杂度
    if complexity_map is None:
        complexity_map = compute_module_complexity_map(source_dir)

    # 收集所有场景名
    scenes: set[str] = set()
    for module_priorities in priority_matrix.values():
        scenes.update(module_priorities.keys())

    results: list[SceneComplexityAlignment] = []

    for scene in sorted(scenes):
        alignment = SceneComplexityAlignment(scene=scene)

        # 计算当前排序: 按 priority 升序 (低→高, 高=靠近用户)
        ordering_raw: list[tuple[str, float, float]] = []
        for module_name, priorities in priority_matrix.items():
            priority = priorities.get(scene, priorities.get("default", 0))
            complexity = complexity_map.get(module_name, 0.0)
            ordering_raw.append((module_name, float(priority), complexity))

        # 按优先级升序排序 (低优先级在前=远离用户, 高优先级在后=靠近用户)
        ordering_raw.sort(key=lambda x: x[1])
        alignment.ordering = ordering_raw

        # 加权复杂度: 优先级越高(靠近用户)的模块复杂度权重越大
        max_priority = max((p for _, p, _ in ordering_raw), default=1)
        for module_name, priority, complexity in ordering_raw:
            weight = priority / max_priority if max_priority > 0 else 0
            alignment.weighted_complexity += weight * complexity

        # 检测复杂度倒挂: 相邻模块中, 前者(低优先级)复杂度远高于后者(高优先级)
        for i in range(len(ordering_raw) - 1):
            curr_name, curr_pri, curr_cmp = ordering_raw[i]
            next_name, next_pri, next_cmp = ordering_raw[i + 1]
            if curr_cmp - next_cmp > inversion_threshold and curr_pri < next_pri:
                alignment.inversions.append(Inversion(
                    high_complexity_module=curr_name,
                    low_complexity_module=next_name,
                    high_priority=curr_pri,
                    low_priority=next_pri,
                    high_complexity=curr_cmp,
                    low_complexity=next_cmp,
                ))

        # 检测复杂度集中: 连续 2+ 个高复杂度模块 (> 平均值)
        avg_complexity = sum(c for _, _, c in ordering_raw) / len(ordering_raw) if ordering_raw else 0
        consecutive_high = 0
        consecutive_modules: list[str] = []
        consecutive_start = 0
        for i, (name, _, cmp) in enumerate(ordering_raw):
            if cmp > avg_complexity:
                if consecutive_high == 0:
                    consecutive_start = i
                    consecutive_modules = [name]
                else:
                    consecutive_modules.append(name)
                consecutive_high += 1
                if consecutive_high >= 2:
                    alignment.concentrations.append(Concentration(
                        position=consecutive_start,
                        modules=list(consecutive_modules),
                        avg_complexity=avg_complexity,
                    ))
            else:
                consecutive_high = 0
                consecutive_modules = []

        # 检测场景不匹配: 最高优先级组中无高复杂度模块
        # 改进: 当多个模块共享最高优先级时, 只要组内有高复杂度模块即视为对齐
        # (Hecate: 高复杂度模块靠近用户输入即可, 不要求所有模块都高)
        if ordering_raw:
            max_priority = ordering_raw[-1][1]
            top_group = [m for m in ordering_raw if m[1] == max_priority]
            top_group_max_complexity = max((c for _, _, c in top_group), default=0)
            if top_group_max_complexity < avg_complexity * 0.5 and avg_complexity > 0:
                # 最高优先级组中所有模块都低复杂度 → 不匹配
                key_module = min(top_group, key=lambda m: m[2])
                alignment.mismatches.append(Mismatch(
                    scene=scene,
                    key_module=key_module[0],
                    key_complexity=key_module[2],
                    avg_complexity=avg_complexity,
                ))

        results.append(alignment)

    return results


def recommend_priority_adjustment(
        alignments: list[SceneComplexityAlignment],
        ) -> dict[str, dict[str, int]]:
    """基于复杂度对齐分析, 推荐优先级矩阵调整 (观测工具, 不自动应用).

    ⚠️ 设计定位: 此函数仅作为观测/诊断工具, 不应自动驱动矩阵调整.
       优先级矩阵的权威来源是 prompt_builder._MODULE_SCENE_PRIORITY,
       其设计原则是"功能性优先" (按场景相关性排布).
       本函数的推荐仅供人工审查参考, 用于发现潜在排序异常 (如倒挂).

    策略:
      1. 复杂度倒挂 → 提升高复杂度模块的优先级 (+1)
         (让高复杂度模块更靠近用户输入, 提升 LLM 注意力)
      2. 复杂度集中 → 降低次要模块优先级 (-1)
         (拆散聚集, 避免 prompt 局部过载)
      3. 场景不匹配 → 提升场景关键模块优先级 (+2)
         (确保场景目标模块靠近用户)

    Returns:
        推荐的新优先级矩阵 {module: {scene: adjusted_priority}}
    """
    # 从第一个 alignment 重建当前矩阵
    if not alignments:
        return {}

    current_matrix: dict[str, dict[str, int]] = {}
    for alignment in alignments:
        for module_name, priority, _ in alignment.ordering:
            if module_name not in current_matrix:
                current_matrix[module_name] = {}
            current_matrix[module_name][alignment.scene] = int(priority)

    recommended = {m: dict(s) for m, s in current_matrix.items()}

    # 应用调整
    for alignment in alignments:
        scene = alignment.scene

        # 1. 复杂度倒挂 → 提升高复杂度模块优先级
        for inv in alignment.inversions:
            module_name = inv.high_complexity_module  # 高复杂度但低优先级的模块
            old_val = recommended[module_name].get(scene, 5)
            recommended[module_name][scene] = min(10, old_val + 1)

        # 2. 复杂度集中 → 拆散聚集
        # 集中模块组中除最后一个外, 其余降低优先级 (最后一个保留靠近用户)
        for conc in alignment.concentrations:
            # 降低集中组中除最后一个外的所有模块优先级
            for module_name in conc.modules[:-1]:
                old_val = recommended[module_name].get(scene, 5)
                if old_val > 1:
                    recommended[module_name][scene] = old_val - 1

        # 3. 场景不匹配 → 提升关键模块优先级
        for mismatch in alignment.mismatches:
            module_name = mismatch.key_module
            old_val = recommended[module_name].get(scene, 5)
            recommended[module_name][scene] = min(10, old_val + 2)

    return recommended


def generate_alignment_report(source_dir: Path | str) -> str:
    """生成场景排序 × 复杂度对齐报告.

    整合 prompt_builder 的场景感知排序和 prompt_complexity 的结构广度分析,
    提供可操作的优先级矩阵优化建议.

    Args:
        source_dir: 项目根目录

    Returns:
        可读的对齐报告字符串
    """
    source_path = Path(source_dir)
    complexity_map = compute_module_complexity_map(source_path)
    alignments = analyze_scene_complexity_alignment(source_path, complexity_map=complexity_map)

    lines = [
        "=" * 60,
        "  场景排序 × 复杂度对齐报告",
        "  (prompt_builder 场景感知排序 × Hecate 结构广度)",
        "=" * 60,
        "",
        "【模块复杂度】",
    ]

    for module, score in sorted(complexity_map.items(), key=lambda x: x[1], reverse=True):
        bar = "█" * int(score * 5) + "░" * max(0, 20 - int(score * 5))
        lines.append(f"  {module:<14} {score:.2f} {bar}")

    lines.extend(["", "【场景对齐分析】"])

    total_aligned = 0
    total_scenes = len(alignments)
    for alignment in alignments:
        status = "✓ 对齐" if alignment.is_aligned else "⚠ 待优化"
        lines.append(f"\n  场景: {alignment.scene}  {status}")
        lines.append(f"    加权复杂度: {alignment.weighted_complexity:.3f}")

        # 打印排序
        lines.append("    排序 (远→近):")
        for module_name, priority, complexity in alignment.ordering:
            cmp_marker = "★" if complexity > 2.0 else " "
            lines.append(
                f"      {cmp_marker} {module_name:<14} "
                f"优先级={priority:.0f}  复杂度={complexity:.2f}"
            )

        if alignment.inversions:
            lines.append(f"    倒挂 ({len(alignment.inversions)}):")
            for inv in alignment.inversions:
                lines.append(f"      ⚠ {inv}")

        if alignment.concentrations:
            lines.append(f"    集中 ({len(alignment.concentrations)}):")
            for conc in alignment.concentrations:
                lines.append(f"      ⚠ {conc}")

        if alignment.mismatches:
            lines.append(f"    不匹配 ({len(alignment.mismatches)}):")
            for mm in alignment.mismatches:
                lines.append(f"      ⚠ {mm}")

        if alignment.is_aligned:
            total_aligned += 1

    # 推荐调整
    recommended = recommend_priority_adjustment(alignments)
    if recommended:
        lines.extend(["", "【推荐优先级调整】"])
        # 找出有变化的条目
        try:
            import prompt_builder
            current = prompt_builder._MODULE_SCENE_PRIORITY
        except (ImportError, AttributeError):
            current = {}

        changes = 0
        for module, scenes in recommended.items():
            for scene, new_val in scenes.items():
                old_val = current.get(module, {}).get(scene, new_val)
                if old_val != new_val:
                    arrow = "↑" if new_val > old_val else "↓"
                    lines.append(
                        f"  {module}[{scene}]: {old_val} →{arrow} {new_val} "
                        f"(Δ{new_val - old_val:+d})"
                    )
                    changes += 1
        if changes == 0:
            lines.append("  (无需调整, 当前矩阵已与复杂度对齐)")
        else:
            lines.append(f"\n  共 {changes} 处调整建议")

    lines.extend([
        "",
        "【总结】",
        f"  场景对齐率: {total_aligned}/{total_scenes} = {total_aligned/total_scenes:.0%}",
        f"  复杂度倒挂: {sum(len(a.inversions) for a in alignments)}",
        f"  复杂度集中: {sum(len(a.concentrations) for a in alignments)}",
        f"  场景不匹配: {sum(len(a.mismatches) for a in alignments)}",
        "",
        "=" * 60,
    ])

    return "\n".join(lines)