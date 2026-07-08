"""XP 驱动的用户画像学习器

将用户的交互行为自动写入 USER.md，让 agent 随 XP 增长逐步深入认识用户。

双层学习机制：
1. **统计层**（零成本）：每条消息自动更新交互统计（消息量、活跃时段、对话深度）
2. **认知层**（LLM 辅助）：每积累 N 条新消息，调用 LLM 从近期对话中抽取用户特征

XP 等级控制认知深度（累加模式，高等级包含所有低等级内容）：
- LV1（陌生人）：基础交互频率、兴趣话题、表达方式
- LV2（熟人）：+活跃时段、对话深度、沟通节奏
- LV3（朋友）：+兴趣领域、沟通风格、技术偏好、情绪触发点
- LV4（挚友）：+性格特点、情感表达、决策风格、价值观倾向
- LV5（灵魂伴侣）：+深层价值观、情感需求、人格特质、内心世界
- LV6（至死不渝）：+人生哲学、深层恐惧与渴望、思维模式、灵魂本质

写入 USER.md 的 `## XP 动态认知` 区块，与用户手动编辑的区域互不干扰。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    from config import DATA_DIR
except Exception:
    logger.debug("user_profile_learner.DATA_DIR_fallback: {}", exc_info=True)
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 常量 ───────────────────────────────────────────────────

# 每多少条新消息触发一次 LLM 认知抽取
_INSIGHT_INTERVAL = 20

# 认知抽取使用的 LLM 提示（简洁、低成本）
_INSIGHT_PROMPT_TEMPLATE = """你是一个用户画像分析器。根据以下近期对话摘要，用 1-3 句话总结你观察到的关于用户的特征。

关注方向（根据等级选择深度）：
{focus_areas}

要求：
- 只写你确实观察到的，不要猜测
- 用中文，简洁自然，像笔记一样
- 不要写"用户"二字，直接描述特征
- 输出纯文本，不要 markdown 格式

近期对话摘要：
{conversation_summary}"""

# 各等级的关注深度（累加模式：高等级包含所有低等级的内容 + 新增深度）
_LEVEL_FOCUS = {
    1: "用户的活跃程度、对话频率、感兴趣的话题、常用表达方式",
    2: "用户的活跃时段、对话深度、常见话题、沟通节奏偏好、语言风格",
    3: "用户的兴趣领域、沟通风格、表达习惯、技术偏好、情绪触发点",
    4: "用户的性格特点、情感表达方式、决策风格、压力应对模式、价值观倾向",
    5: "用户的深层价值观、情感需求、人格特质、与 agent 的互动模式、内心世界",
    6: "用户的人生哲学、深层恐惧与渴望、思维模式、创造力表达、成长轨迹、灵魂本质",
}


class UserProfileLearner:
    """用户画像学习器（单例）"""

    def __init__(self, data_dir: Path | None = None):
        self._data_path = (Path(data_dir) if data_dir else Path(DATA_DIR)) / "user_profile_stats.json"
        self._stats: dict = {}
        self._load()

    # ── 持久化 ──────────────────────────────────────────────

    def _load(self):
        if self._data_path.exists():
            try:
                with open(self._data_path, encoding="utf-8") as f:
                    self._stats = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._stats = {}

    def _save(self):
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._data_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._stats, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._data_path)

    # ── 统计层（零成本，每条消息调用） ────────────────────────

    def record_interaction(self, user_id: str, message_length: int,
                           is_deep: bool = False) -> dict:
        """记录一次交互的统计数据。返回该用户当前统计。"""
        s = self._stats.setdefault(user_id, {
            "total_messages": 0,
            "deep_messages": 0,
            "total_chars": 0,
            "hour_distribution": {},
            "last_active": 0,
            "first_seen": time.time(),
            "new_since_insight": 0,
            "last_insight_at": 0,
            "last_insight_text": "",
        })

        s["total_messages"] += 1
        s["total_chars"] += message_length
        if is_deep:
            s["deep_messages"] += 1

        # 活跃时段分布
        hour = time.strftime("%H")
        s["hour_distribution"][hour] = s["hour_distribution"].get(hour, 0) + 1

        s["last_active"] = time.time()
        s["new_since_insight"] = s.get("new_since_insight", 0) + 1

        self._save()
        return s

    def should_run_insight(self, user_id: str) -> bool:
        """判断是否应该触发 LLM 认知抽取。"""
        s = self._stats.get(user_id)
        if not s:
            return False
        return s.get("new_since_insight", 0) >= _INSIGHT_INTERVAL

    def get_stats(self, user_id: str) -> dict:
        return self._stats.get(user_id, {})

    # ── 认知层（由调用方传入 LLM 响应） ──────────────────────

    def save_insight(self, user_id: str, insight_text: str, xp_level: int = 1) -> str:
        """保存从 LLM 获得的用户认知结果。

        由 message_processor 调用 LLM 后传入结果。

        Args:
            user_id: 用户 ID
            insight_text: LLM 返回的认知文本
            xp_level: 当前 XP 等级 (1-6)

        Returns:
            实际保存的文本，无效输入返回空字符串
        """
        s = self._stats.get(user_id)
        if not s:
            return ""

        insight_text = insight_text.strip()
        if not insight_text or len(insight_text) < 5:
            return ""

        # 更新统计
        s["new_since_insight"] = 0
        s["last_insight_at"] = time.time()
        s["last_insight_text"] = insight_text
        self._save()

        # 写入 USER.md
        self._write_to_user_md(insight_text, xp_level)

        logger.info(f"UserProfileLearner.insight_saved user={user_id} lv={xp_level} len={len(insight_text)}")
        return insight_text

    @staticmethod
    def build_insight_prompt(recent_messages: list[dict], xp_level: int = 1) -> str:
        """构建用于 LLM 认知抽取的提示词。

        由调用方使用此提示词调用 LLM，再将结果传入 save_insight()。
        """
        summary_lines = []
        for msg in recent_messages[-20:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content", ""))[:200]
            summary_lines.append(f"{role}: {content}")
        conversation_summary = "\n".join(summary_lines)

        focus = _LEVEL_FOCUS.get(xp_level, _LEVEL_FOCUS[1])
        return _INSIGHT_PROMPT_TEMPLATE.format(
            focus_areas=focus,
            conversation_summary=conversation_summary,
        )

    # ── USER.md 读写 ────────────────────────────────────────

    def _get_user_md_path(self) -> Path:
        """获取 USER.md 运行时路径"""
        try:
            from prompt_builder import _workspace_dir
            return _workspace_dir() / "USER.md"
        except Exception:
            logger.debug("user_profile_learner.user_md_path_fallback: {}", exc_info=True)
            return Path.home() / ".ai-agent" / "workspace" / "USER.md"

    def _write_to_user_md(self, insight_text: str, xp_level: int):
        """将认知结果写入 USER.md 的 `## XP 动态认知` 区块。"""
        path = self._get_user_md_path()
        if not path.exists():
            return

        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return

        # 构建新的认知区块
        now_str = time.strftime("%Y-%m-%d %H:%M")
        level_names = {1: "陌生人", 2: "熟人", 3: "朋友", 4: "挚友", 5: "灵魂伴侣", 6: "至死不渝"}
        level_name = level_names.get(xp_level, "未知")

        new_block = (
            f"## XP 动态认知\n"
            f"\n"
            f"> 当前关系：LV{xp_level} {level_name} · 最后更新：{now_str}\n"
            f"\n"
            f"{insight_text}\n"
        )

        # 查找已有的 `## XP 动态认知` 区块并替换
        pattern = r"## XP 动态认知\n.*?(?=\n## |\Z)"
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, new_block.rstrip("\n"), content, flags=re.DOTALL)
        else:
            # 追加到文件末尾
            content = content.rstrip("\n") + "\n\n" + new_block

        # 原子写入
        tmp = path.with_suffix(".md.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            logger.info(f"UserProfileLearner.user_md_updated path={path}")
        except OSError as e:
            logger.warning(f"UserProfileLearner.user_md_write_failed: {e}")

    # ── 读取认知（供 prompt_builder 使用） ────────────────────

    def get_learned_insight(self, user_id: str) -> str:
        """返回该用户最近一次 LLM 认知抽取的结果文本。"""
        s = self._stats.get(user_id)
        if s and s.get("last_insight_text"):
            return s["last_insight_text"]
        return ""

    def get_stats_summary(self, user_id: str) -> str:
        """生成交互统计的文字摘要，供 prompt 注入。"""
        s = self._stats.get(user_id)
        if not s:
            return ""

        total = s.get("total_messages", 0)
        deep = s.get("deep_messages", 0)
        chars = s.get("total_chars", 0)
        avg_len = chars // max(total, 1)

        # 最活跃时段
        dist = s.get("hour_distribution", {})
        if dist:
            top_hours = sorted(dist.items(), key=lambda x: x[1], reverse=True)[:3]
            hour_str = "、".join(f"{h}:00" for h, _ in top_hours)
        else:
            hour_str = "未知"

        return (
            f"累计对话 {total} 次，深度对话 {deep} 次，"
            f"平均消息长度 {avg_len} 字，"
            f"最活跃时段：{hour_str}"
        )


# ── 单例 ──────────────────────────────────────────────────

_singleton: UserProfileLearner | None = None


def get_user_profile_learner() -> UserProfileLearner:
    """获取用户画像学习器单例。"""
    global _singleton
    if _singleton is None:
        _singleton = UserProfileLearner()
    return _singleton
