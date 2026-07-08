"""学习反馈闭环 (A4 - P1 自我意识)

将 Agent 的成功/失败经验转化为可复用的语言化记忆:
- 记录 LearningEvent (SUCCESS / FAILURE / PARTIAL / USER_FEEDBACK)
- 从事件中提取教训 (什么方法有效 / 无效 / 部分有效 / 用户偏好)
- 相似教训合并 (字符串相似度 > 0.8 时合并并增加 confidence)
- 持久化到 DATA_DIR/learning_feedback.json
- 策略表: task_pattern → strategy_update, 供后续推理参考

设计原则:
- 轻量: 关键词匹配检索, 不依赖向量检索
- 可插拔: 不修改既有模块接口, 由 Agent-R 反思器/工具执行器主动调用
- 幂等: 重复持久化安全, 加载失败回退到空状态
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Optional

from loguru import logger

# 延迟导入 DATA_DIR, 避免 config 模块在测试中导入失败时影响本模块
try:
    from config import DATA_DIR
except Exception:  # pragma: no cover - 配置缺失时退化为项目根目录
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)


class EventType(str, Enum):
    """学习事件类型"""
    SUCCESS = "success"             # 成功: 记录有效策略
    FAILURE = "failure"             # 失败: 记录失败原因
    PARTIAL = "partial"             # 部分成功: 记录部分有效的原因
    USER_FEEDBACK = "user_feedback"  # 用户反馈: 记录用户偏好


# 教训相似度阈值, 超过此值则视为重复教训并合并
_SIMILARITY_THRESHOLD = 0.8
# 默认 confidence 上限, 避免无限增长
_MAX_CONFIDENCE = 10.0
# 内存中保留的最大教训条数
_MAX_LESSONS = 200


@dataclass
class LearningEvent:
    """学习事件

    Attributes:
        event_type: 事件类型 (success/failure/partial/user_feedback)
        task_description: 任务描述
        approach_used: 使用的方法/策略
        outcome: 结果描述
        duration: 耗时 (秒)
        lessons: 教训列表 (可由 extract_lessons 填充)
        timestamp: 事件时间戳
        metadata: 附加元数据
    """
    event_type: EventType
    task_description: str
    approach_used: str = ""
    outcome: str = ""
    duration: float = 0.0
    lessons: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class Lesson:
    """教训

    Attributes:
        content: 教训文本
        event_type: 来源事件类型
        confidence: 置信度 (基于重复次数累加, 上限 _MAX_CONFIDENCE)
        last_updated: 最后更新时间
        occurrence_count: 出现次数
    """
    content: str
    event_type: EventType
    confidence: float = 1.0
    last_updated: float = field(default_factory=time.time)
    occurrence_count: int = 1

    def to_dict(self) -> dict:
        """将教训序列化为字典 (用于持久化)."""
        return {
            "content": self.content,
            "event_type": self.event_type.value,
            "confidence": self.confidence,
            "last_updated": self.last_updated,
            "occurrence_count": self.occurrence_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Lesson:
        """从字典反序列化为 Lesson 实例.

        Args:
            d: 由 to_dict 生成的字典

        Returns:
            重建的 Lesson 实例
        """
        return cls(
            content=d.get("content", ""),
            event_type=EventType(d.get("event_type", "success")),
            confidence=float(d.get("confidence", 1.0)),
            last_updated=float(d.get("last_updated", time.time())),
            occurrence_count=int(d.get("occurrence_count", 1)),
        )


def _similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度 (0~1)

    使用 difflib.SequenceMatcher, 基于最长公共子序列比例。
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _tokenize(text: str) -> set[str]:
    """简单分词: 小写化 + 按非字母数字切分, 保留长度 >= 2 的 token"""
    tokens = set()
    for raw in text.lower().split():
        # 去除标点
        clean = "".join(c for c in raw if c.isalnum())
        if len(clean) >= 2:
            tokens.add(clean)
    return tokens


class LearningFeedbackLoop:
    """学习反馈闭环

    用法:
        loop = LearningFeedbackLoop()
        loop.record(LearningEvent(
            event_type=EventType.FAILURE,
            task_description="call web_search",
            approach_used="direct call",
            outcome="timeout after 15s",
        ))
        lessons = loop.get_relevant_lessons("call web_search")
        loop.update_strategy("web_search timeout", "increase timeout to 30s")
        loop.persist()
    """

    def __init__(self, persist_path: Path | None = None,
                 max_lessons: int = _MAX_LESSONS) -> None:
        self._lessons: list[Lesson] = []
        self._strategies: dict[str, str] = {}
        self._max_lessons = max_lessons
        if persist_path is not None:
            self._persist_path = Path(persist_path)
        else:
            self._persist_path = Path(DATA_DIR) / "learning_feedback.json"
        # 启动时尝试加载已有数据, 失败则保持空状态
        try:
            self.load()
        except Exception as e:  # pragma: no cover - 加载失败不影响功能
            logger.warning(f"LearningFeedback.load_failed: {e}")

    # ── 记录与提取 ──────────────────────────────────────────

    def record(self, event: LearningEvent) -> list[Lesson]:
        """记录一个学习事件, 提取教训并合并到内存

        Returns:
            本次记录新增或更新的 Lesson 列表
        """
        if not event.lessons:
            event.lessons = self.extract_lessons(event)
        added: list[Lesson] = []
        for content in event.lessons:
            if not content or not content.strip():
                continue
            merged = self._merge_or_add(content, event.event_type)
            if merged is not None:
                added.append(merged)
        # 超过容量时按 confidence 升序淘汰
        if len(self._lessons) > self._max_lessons:
            self._lessons.sort(key=lambda x: (x.confidence, x.last_updated))
            drop = len(self._lessons) - self._max_lessons
            self._lessons = self._lessons[drop:]
        logger.info(
            f"LearningFeedback.record type={event.event_type.value} "
            f"task={event.task_description[:60]} added={len(added)}"
        )
        return added

    def extract_lessons(self, event: LearningEvent) -> list[str]:
        """从事件中提取教训

        - SUCCESS: 记录什么方法有效
        - FAILURE: 记录什么方法无效, 为什么
        - PARTIAL: 记录部分成功的原因
        - USER_FEEDBACK: 记录用户偏好
        """
        et = event.event_type
        task = event.task_description.strip() or "<unknown task>"
        approach = event.approach_used.strip() or "<unspecified approach>"
        outcome = event.outcome.strip() or "<no outcome>"

        if et == EventType.SUCCESS:
            return [
                f"Approach '{approach}' is effective for tasks like '{task}'. "
                f"Outcome: {outcome}."
            ]
        if et == EventType.FAILURE:
            return [
                f"Approach '{approach}' failed for tasks like '{task}'. "
                f"Reason: {outcome}. Avoid repeating this approach."
            ]
        if et == EventType.PARTIAL:
            return [
                f"Approach '{approach}' partially worked for '{task}'. "
                f"Partial outcome: {outcome}. Refine the failing part."
            ]
        if et == EventType.USER_FEEDBACK:
            return [
                f"User feedback on '{task}': {outcome}. "
                f"Align future behavior with this preference."
            ]
        return [f"Lesson for '{task}': {outcome}"]

    def get_relevant_lessons(self, task_description: str,
                              top_k: int = 3) -> list[Lesson]:
        """根据 task_description 检索相关教训

        使用简单关键词匹配 (Jaccard 相似度), 不依赖向量检索。
        返回最相关的 top_k 教训 (按相关度 + confidence 排序)。
        """
        if not self._lessons or not task_description.strip():
            return []
        query_tokens = _tokenize(task_description)
        if not query_tokens:
            return []

        scored: list[tuple[float, Lesson]] = []
        for lesson in self._lessons:
            lesson_tokens = _tokenize(lesson.content)
            if not lesson_tokens:
                continue
            # Jaccard 相似度
            inter = len(query_tokens & lesson_tokens)
            union = len(query_tokens | lesson_tokens)
            relevance = inter / union if union else 0.0
            # 综合分: 相关度 * 0.7 + confidence * 0.3
            score = relevance * 0.7 + min(lesson.confidence, _MAX_CONFIDENCE) / _MAX_CONFIDENCE * 0.3
            if score > 0:
                scored.append((score, lesson))

        scored.sort(key=lambda x: (x[0], x[1].confidence), reverse=True)
        return [lesson for _, lesson in scored[:top_k]]

    # ── 策略表 ──────────────────────────────────────────────

    def update_strategy(self, task_pattern: str, strategy_update: str) -> None:
        """更新 task_pattern 对应的策略"""
        if not task_pattern or not strategy_update:
            return
        self._strategies[task_pattern] = strategy_update
        logger.info(
            f"LearningFeedback.update_strategy pattern={task_pattern[:60]}"
        )

    def get_strategy(self, task_pattern: str) -> str | None:
        """获取 task_pattern 对应的策略, 支持精确匹配与子串包含"""
        if not task_pattern:
            return None
        # 精确匹配
        if task_pattern in self._strategies:
            return self._strategies[task_pattern]
        # 子串包含 (task_pattern 作为 key 的子串或反之)
        for key, val in self._strategies.items():
            if task_pattern in key or key in task_pattern:
                return val
        return None

    # ── 持久化 ──────────────────────────────────────────────

    def persist(self) -> Path:
        """持久化到 JSON 文件 (DATA_DIR/learning_feedback.json)"""
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "lessons": [l.to_dict() for l in self._lessons],
            "strategies": dict(self._strategies),
            "updated_at": time.time(),
        }
        tmp = self._persist_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._persist_path)
        logger.info(
            f"LearningFeedback.persist path={self._persist_path} "
            f"lessons={len(self._lessons)} strategies={len(self._strategies)}"
        )
        return self._persist_path

    def load(self) -> None:
        """从 JSON 文件加载, 文件不存在或格式错误时保持空状态"""
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                data = json.load(f)
            self._lessons = [Lesson.from_dict(d) for d in data.get("lessons", [])]
            self._strategies = dict(data.get("strategies", {}))
            logger.info(
                f"LearningFeedback.load path={self._persist_path} "
                f"lessons={len(self._lessons)} strategies={len(self._strategies)}"
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"LearningFeedback.load corrupted: {e}")
            self._lessons = []
            self._strategies = {}

    # ── 内部: 相似教训合并 ──────────────────────────────────

    def _merge_or_add(self, content: str, event_type: EventType) -> Lesson | None:
        """合并相似教训或新增

        相似度 > _SIMILARITY_THRESHOLD 时合并:
        - confidence += 1 (上限 _MAX_CONFIDENCE)
        - occurrence_count += 1
        - last_updated 刷新
        - event_type 取较严重者 (FAILURE 优先)
        """
        for existing in self._lessons:
            if _similarity(existing.content, content) > _SIMILARITY_THRESHOLD:
                existing.confidence = min(_MAX_CONFIDENCE, existing.confidence + 1.0)
                existing.occurrence_count += 1
                existing.last_updated = time.time()
                # 事件类型优先级: FAILURE > PARTIAL > USER_FEEDBACK > SUCCESS
                if _event_severity(event_type) > _event_severity(existing.event_type):
                    existing.event_type = event_type
                return existing
        new_lesson = Lesson(content=content, event_type=event_type)
        self._lessons.append(new_lesson)
        return new_lesson

    # ── 辅助: 查询接口 ─────────────────────────────────────

    def get_all_lessons(self) -> list[Lesson]:
        """返回所有教训 (按 confidence 降序)"""
        return sorted(self._lessons, key=lambda x: x.confidence, reverse=True)

    def get_all_strategies(self) -> dict[str, str]:
        """返回所有策略"""
        return dict(self._strategies)

    def get_stats(self) -> dict:
        """统计信息"""
        by_type: dict[str, int] = {}
        for l in self._lessons:
            by_type[l.event_type.value] = by_type.get(l.event_type.value, 0) + 1
        return {
            "total_lessons": len(self._lessons),
            "total_strategies": len(self._strategies),
            "by_event_type": by_type,
            "persist_path": str(self._persist_path),
        }


def _event_severity(et: EventType) -> int:
    """事件严重度排序: FAILURE 最高, SUCCESS 最低"""
    return {
        EventType.FAILURE: 4,
        EventType.PARTIAL: 3,
        EventType.USER_FEEDBACK: 2,
        EventType.SUCCESS: 1,
    }.get(et, 0)


# ── 单例 (供 Agent-R 反思器与工具执行器共享) ──────────────

_singleton: LearningFeedbackLoop | None = None


def get_learning_feedback_loop() -> LearningFeedbackLoop:
    """获取全局单例 LearningFeedbackLoop"""
    global _singleton
    if _singleton is None:
        _singleton = LearningFeedbackLoop()
    return _singleton


def record_tool_outcome(tool_name: str, arguments: dict,
                        success: bool, error: str = "",
                        duration: float = 0.0,
                        task_description: str = "") -> None:
    """工具执行结果 → 学习事件 (供 ToolExecutor 集成)

    轻量封装: 不抛异常, 失败时仅记录日志。
    """
    try:
        loop = get_learning_feedback_loop()
        args_brief = ", ".join(f"{k}={v!r}" for k, v in list(arguments.items())[:3])
        task = task_description or f"tool:{tool_name}({args_brief})"
        event = LearningEvent(
            event_type=EventType.SUCCESS if success else EventType.FAILURE,
            task_description=task,
            approach_used=f"{tool_name}({args_brief})",
            outcome="ok" if success else (error or "failed"),
            duration=duration,
            metadata={"tool": tool_name, "arguments": arguments},
        )
        loop.record(event)
    except Exception as e:  # pragma: no cover - 不影响主流程
        logger.warning(f"LearningFeedback.record_tool_outcome_failed: {e}")


def record_reflection_lesson(lesson_text: str,
                               pattern: str = "",
                               correction: str = "") -> None:
    """Agent-R 反思记忆 → 学习事件 (供 AgentRReflector 集成)

    把反思器生成的 lesson/correction 注入到学习闭环中。
    不修改 AgentRReflector 接口, 由调用方主动调用。
    """
    try:
        loop = get_learning_feedback_loop()
        full = lesson_text
        if correction:
            full = f"{lesson_text} → {correction}"
        event = LearningEvent(
            event_type=EventType.FAILURE,
            task_description=pattern or "agent_r_reflection",
            approach_used="",
            outcome=full,
            lessons=[full],
            metadata={"source": "agent_r_reflection", "pattern": pattern},
        )
        loop.record(event)
    except Exception as e:  # pragma: no cover - 不影响主流程
        logger.warning(f"LearningFeedback.record_reflection_lesson_failed: {e}")
