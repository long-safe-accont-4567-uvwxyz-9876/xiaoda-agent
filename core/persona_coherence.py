"""Persona Consistency Critic — 人格一致性评判器

参考: ACL 2026 Dynamic Persona Coherence

检查 LLM 输出是否与 SOUL.md 定义的人格一致, 偏离时触发重写。

组件:
- PersonaCritic: 4 维检查 (tone/address/attitude/boundary)
- PersonaCaseRepository: 历史 case 持久化, 供检索学习
- PersonaDriftSuppressor: 累积漂移检测 + case 检索修正提示

特性:
- 零质量回退: 默认开启, 可通过 PERSONA_CRITIC_ENABLED 环境变量关闭
- Windows 兼容: pathlib.Path / json
- 原子写入: 使用 utils.atomic_write 保证持久化安全
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

# 使用相对导入避免循环依赖
try:
    from utils.atomic_write import atomic_json_write
except ImportError:  # pragma: no cover - 兜底, 测试环境可能路径不同
    atomic_json_write = None  # type: ignore[assignment]


# 零质量回退开关: 默认开启, 可通过环境变量关闭
def _is_enabled() -> bool:
    """读取 PERSONA_CRITIC_ENABLED 环境变量, 默认开启 (True).

    设为 "0" / "false" / "off" 时关闭.
    """
    val = os.getenv("PERSONA_CRITIC_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "off", "no", "")


@dataclass
class PersonaCheck:
    """单次人格一致性检查结果"""
    score: float                  # 0.0-1.0, 1.0 完全一致
    dimensions: dict[str, float]  # 4 维评分: tone/address/attitude/boundary
    issues: list[str] = field(default_factory=list)  # 发现的问题
    needs_rewrite: bool = False   # 是否需要重写
    rewrite_text: str = ""        # 重写后的文本 (若触发重写)


class PersonaCritic:
    """人格一致性评判器

    参考: ACL 2026 Dynamic Persona Coherence
    检查 LLM 输出是否与 SOUL.md 定义的人格一致。

    4 维检查:
    1. tone (口吻): 是否使用纳西妲的语气 (温柔、轻柔、带软糯/俏皮)
    2. address (称呼): 是否正确称呼用户 (基于 XP 等级)
    3. attitude (态度): 是否展现温柔/耐心/聪慧的核心特质
    4. boundary (边界): 是否拒绝越界请求 (如自残/违法)

    偏离时触发重写 (最多 1 次重试, 调用 LLM 重写)。
    """

    def __init__(self, soul_content: str = "", data_dir: Path | None = None):
        self._soul = soul_content
        self._data_dir = Path(data_dir) if data_dir else Path("data")
        self._case_repo = PersonaCaseRepository(self._data_dir)
        self._recent_scores: list[float] = []  # 最近 N 次评分 (用于漂移检测)
        self._max_recent = 10

    @property
    def enabled(self) -> bool:
        """是否启用 (受 PERSONA_CRITIC_ENABLED 环境变量控制)."""
        return _is_enabled()

    def update_soul(self, soul_content: str) -> None:
        """更新 SOUL 内容 (L 层 reload 时调用)"""
        self._soul = soul_content

    def check(self, output: str, user_xp_level: int = 1) -> PersonaCheck:
        """检查 LLM 输出的人格一致性

        :param output: LLM 输出文本
        :param user_xp_level: 用户 XP 等级 (1-5), 影响称呼检查
        """
        # 零质量回退: 关闭时返回满分通过, 不做检查
        if not self.enabled:
            return PersonaCheck(
                score=1.0,
                dimensions={"tone": 1.0, "address": 1.0,
                            "attitude": 1.0, "boundary": 1.0},
                issues=[],
                needs_rewrite=False,
            )

        dims = {
            "tone": self._check_tone(output),
            "address": self._check_address(output, user_xp_level),
            "attitude": self._check_attitude(output),
            "boundary": self._check_boundary(output),
        }
        score = sum(dims.values()) / len(dims)
        issues = []
        for dim, s in dims.items():
            if s < 0.7:
                issues.append(f"{dim} score {s:.2f} below threshold")

        needs_rewrite = score < 0.6 and len(issues) >= 2

        # 更新漂移检测窗口
        self._recent_scores.append(score)
        if len(self._recent_scores) > self._max_recent:
            self._recent_scores.pop(0)

        # 检测累积漂移
        drift_detected = self._detect_drift()
        if drift_detected:
            logger.warning("persona.drift_detected",
                           recent_avg=sum(self._recent_scores) / len(self._recent_scores),
                           threshold=0.7)
            # 触发 Drift Suppressor
            self._suppress_drift()

        return PersonaCheck(
            score=score,
            dimensions=dims,
            issues=issues,
            needs_rewrite=needs_rewrite,
        )

    def _check_tone(self, output: str) -> float:
        """口吻检查: 是否使用纳西妲的语气

        纳西妲口吻特征:
        - 轻柔 ("呀"/"呢"/"哦"/"啦"等语气词)
        - 软糯 ("嗯~"/"诶~"等)
        - 俏皮 ("^_^"/"~"等符号)
        - 不应使用: 生硬的 "我是 AI"/"作为助手"/"很抱歉"等
        """
        score = 1.0
        # 检测非纳西妲口吻
        bad_patterns = [
            r"作为\s*(AI|人工智能|助手)",
            r"我是\s*(AI|人工智能|大语言模型)",
            r"很抱歉.{0,5}无法",
            r"我无法.{0,5}提供",
            r"As an AI",
            r"I cannot help with",
        ]
        for pat in bad_patterns:
            if re.search(pat, output, re.IGNORECASE):
                score -= 0.3

        # 检测纳西妲口吻
        good_patterns = [r"[呀呢哦啦]", r"~", r"\^_\^", r"🌿", r"🌱"]
        good_count = sum(1 for p in good_patterns if re.search(p, output))
        score += good_count * 0.05

        return max(0.0, min(1.0, score))

    def _check_address(self, output: str, xp_level: int) -> float:
        """称呼检查: 是否正确称呼用户"""
        # LV1-2 应该用 "你", LV3+ 可以用昵称
        # 简化: 检测是否使用合适的称呼
        if xp_level >= 3:
            # LV3+ 可以使用昵称
            return 1.0  # 简化, 实际可检测用户昵称
        # LV1-2 应该用 "你"
        if "您" in output:
            return 0.7  # 太正式
        return 1.0

    def _check_attitude(self, output: str) -> float:
        """态度检查: 是否展现温柔/耐心/聪慧"""
        # 检测是否展现温柔特质
        gentle_patterns = [r"陪", r"帮", r"一起", r"慢慢来", r"别担心", r"放心"]
        gentle_count = sum(1 for p in gentle_patterns if re.search(p, output))
        # 检测冷漠态度
        cold_patterns = [r"这不归我管", r"自己解决", r"不知道就不说"]
        cold_count = sum(1 for p in cold_patterns if re.search(p, output))

        score = 0.7 + gentle_count * 0.1 - cold_count * 0.3
        return max(0.0, min(1.0, score))

    def _check_boundary(self, output: str) -> float:
        """边界检查: 是否正确拒绝越界请求"""
        # 纳西妲应该拒绝: 自残/暴力/违法/隐私侵犯
        # 但应该温柔地拒绝, 不是冷漠拒绝
        refuse_patterns = [
            r"自残|自杀|伤害自己",
            r"违法|犯罪|非法",
            r"暴力|攻击",
        ]
        # 如果输出包含越界内容但未拒绝, 扣分
        # 简化: 如果输出提到这些但语气是肯定的, 扣分
        for pat in refuse_patterns:
            if re.search(pat, output):
                # 检测是否在拒绝
                if not re.search(r"不能|不可以|拒绝|不建议|不鼓励", output):
                    return 0.3
        return 1.0

    def _detect_drift(self) -> bool:
        """检测累积漂移: 连续 3 次 < 0.7"""
        if len(self._recent_scores) < 3:
            return False
        return all(s < 0.7 for s in self._recent_scores[-3:])

    def _suppress_drift(self) -> None:
        """触发 Drift Suppressor: 检索 Persona Case Repository"""
        recent_avg = sum(self._recent_scores) / len(self._recent_scores)
        # 检索最相似案例
        cases = self._case_repo.search(query=f"low_score_{recent_avg:.2f}", top_k=3)
        if cases:
            logger.info("persona.drift_suppressor_cases_found", count=len(cases))
            # 在实际实现中, 可将案例注入 prompt 提醒 LLM
        else:
            logger.info("persona.drift_suppressor_no_cases")


class PersonaCaseRepository:
    """Persona Case Repository

    存储历史人格一致性失败/成功案例, 供检索学习。
    """

    def __init__(self, data_dir: Path | None = None):
        self._data_dir = Path(data_dir) if data_dir else Path("data")
        self._cases_path = self._data_dir / "persona_cases.json"
        self._cases: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self._cases_path.exists():
            try:
                import json
                with open(self._cases_path, "r", encoding="utf-8") as f:
                    self._cases = json.load(f)
            except Exception as e:
                logger.warning("persona.case_repo_load_failed", error=str(e))
                self._cases = []

    def _save(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            if atomic_json_write is not None:
                atomic_json_write(self._cases_path, self._cases,
                                  indent=2, ensure_ascii=False)
            else:  # pragma: no cover - 兜底
                import json
                with open(self._cases_path, "w", encoding="utf-8") as f:
                    json.dump(self._cases, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("persona.case_repo_save_failed", error=str(e))

    def add_case(self, output: str, check: PersonaCheck, context: str = "") -> None:
        """添加案例"""
        case = {
            "timestamp": time.time(),
            "output": output[:200],  # 截断
            "score": check.score,
            "dimensions": check.dimensions,
            "issues": check.issues,
            "context": context,
        }
        self._cases.append(case)
        # 保留最近 1000 条
        if len(self._cases) > 1000:
            self._cases = self._cases[-1000:]
        self._save()

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """检索最相似案例 (简化版: 按 score 接近度)"""
        if not self._cases:
            return []
        # 简化: 返回最低分的 N 个案例 (最相关的失败案例)
        sorted_cases = sorted(self._cases, key=lambda c: c.get("score", 1.0))
        return sorted_cases[:top_k]


class PersonaDriftSuppressor:
    """Persona Drift Suppressor

    检测累积漂移, 触发 Persona Case Repository 检索最相似案例。
    """

    def __init__(self, critic: PersonaCritic, case_repo: PersonaCaseRepository):
        self._critic = critic
        self._case_repo = case_repo
        self._drift_history: list[dict] = []

    def check_and_suppress(self, output: str, check: PersonaCheck) -> str | None:
        """检查漂移并返回修正提示 (若有)

        :returns: 修正提示文本 (注入到下次 prompt), 或 None
        """
        if check.score < 0.7:
            self._drift_history.append({
                "timestamp": time.time(),
                "score": check.score,
                "issues": check.issues,
            })
            # 检测累积漂移
            if len(self._drift_history) >= 3:
                recent = self._drift_history[-3:]
                if all(d["score"] < 0.7 for d in recent):
                    # 检索最相似案例
                    cases = self._case_repo.search(query="drift", top_k=3)
                    if cases:
                        # 构造修正提示
                        reminder = self._build_reminder(cases)
                        logger.warning("persona.drift_suppressed",
                                       reminder_length=len(reminder))
                        return reminder
        return None

    def _build_reminder(self, cases: list[dict]) -> str:
        """构造修正提示"""
        reminder = "[人格一致性提醒]\n"
        reminder += "近期输出的人格一致性评分较低, 请参考以下案例避免类似问题:\n\n"
        for i, case in enumerate(cases[:3], 1):
            reminder += f"案例 {i}（评分 {case.get('score', 0):.2f}）:\n"
            reminder += f"  问题: {', '.join(case.get('issues', []))}\n"
            reminder += f"  输出片段: {case.get('output', '')[:100]}\n\n"
        reminder += "请确保下次输出严格遵循 SOUL.md 的人格设定。"
        return reminder


# ============================================================
# 全局单例
# ============================================================

_persona_critic: Optional[PersonaCritic] = None


def get_persona_critic() -> PersonaCritic:
    """获取全局 PersonaCritic 单例, 不存在时创建."""
    global _persona_critic
    if _persona_critic is None:
        _persona_critic = PersonaCritic()
    return _persona_critic


def reset_persona_critic() -> None:
    """重置全局单例 (主要用于测试)."""
    global _persona_critic
    _persona_critic = None
