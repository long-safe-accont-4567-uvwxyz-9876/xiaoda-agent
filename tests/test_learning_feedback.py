"""A4 学习反馈闭环测试

覆盖:
- test_record_success: 记录成功事件
- test_record_failure: 记录失败事件
- test_extract_lessons: 各事件类型教训提取正确
- test_get_relevant_lessons: 关键词匹配检索相关教训
- test_merge_similar_lessons: 相似教训合并并增加 confidence
- test_persist_load: 持久化和加载
- test_update_strategy: 策略更新和获取
- test_confidence_increases: 重复教训增加 confidence
"""
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# 确保项目根在 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.learning_feedback import (
    EventType,
    LearningEvent,
    LearningFeedbackLoop,
    Lesson,
    record_reflection_lesson,
    record_tool_outcome,
)


@pytest.fixture
def tmp_loop(tmp_path):
    """每个测试用独立的临时持久化路径, 避免污染单例"""
    path = tmp_path / "learning_feedback.json"
    return LearningFeedbackLoop(persist_path=path)


# ============================================================
# test_record_success
# ============================================================

def test_record_success(tmp_loop):
    event = LearningEvent(
        event_type=EventType.SUCCESS,
        task_description="call web_search for tokyo weather",
        approach_used="web_search(query='tokyo weather')",
        outcome="returned current weather",
        duration=0.8,
    )
    added = tmp_loop.record(event)
    assert len(added) >= 1
    assert any("effective" in l.content.lower() for l in added)
    assert len(tmp_loop.get_all_lessons()) >= 1


# ============================================================
# test_record_failure
# ============================================================

def test_record_failure(tmp_loop):
    event = LearningEvent(
        event_type=EventType.FAILURE,
        task_description="call shell_command to list files",
        approach_used="shell_command(cmd='ls /nonexistent')",
        outcome="no such directory",
        duration=0.1,
    )
    added = tmp_loop.record(event)
    assert len(added) >= 1
    assert any("failed" in l.content.lower() or "avoid" in l.content.lower() for l in added)
    # 教训类型应为 FAILURE
    assert any(l.event_type == EventType.FAILURE for l in added)


# ============================================================
# test_extract_lessons
# ============================================================

class TestExtractLessons:
    def test_success_lesson(self, tmp_loop):
        event = LearningEvent(
            event_type=EventType.SUCCESS,
            task_description="task_x",
            approach_used="approach_y",
            outcome="done well",
        )
        lessons = tmp_loop.extract_lessons(event)
        assert len(lessons) == 1
        assert "effective" in lessons[0].lower()
        assert "approach_y" in lessons[0]

    def test_failure_lesson(self, tmp_loop):
        event = LearningEvent(
            event_type=EventType.FAILURE,
            task_description="task_x",
            approach_used="approach_y",
            outcome="timeout",
        )
        lessons = tmp_loop.extract_lessons(event)
        assert len(lessons) == 1
        assert "failed" in lessons[0].lower()
        assert "timeout" in lessons[0]

    def test_partial_lesson(self, tmp_loop):
        event = LearningEvent(
            event_type=EventType.PARTIAL,
            task_description="task_x",
            approach_used="approach_y",
            outcome="only got 2 of 5 results",
        )
        lessons = tmp_loop.extract_lessons(event)
        assert len(lessons) == 1
        assert "partial" in lessons[0].lower()

    def test_user_feedback_lesson(self, tmp_loop):
        event = LearningEvent(
            event_type=EventType.USER_FEEDBACK,
            task_description="answer style",
            approach_used="verbose",
            outcome="user prefers concise answers",
        )
        lessons = tmp_loop.extract_lessons(event)
        assert len(lessons) == 1
        assert "user feedback" in lessons[0].lower()
        assert "concise" in lessons[0]


# ============================================================
# test_get_relevant_lessons
# ============================================================

class TestGetRelevantLessons:
    def test_retrieves_matching(self, tmp_loop):
        # 记录一条与 web_search 相关的教训
        tmp_loop.record(LearningEvent(
            event_type=EventType.FAILURE,
            task_description="call web_search for tokyo weather",
            approach_used="web_search",
            outcome="timeout",
        ))
        # 记录一条无关教训
        tmp_loop.record(LearningEvent(
            event_type=EventType.FAILURE,
            task_description="read_file on /etc/passwd",
            approach_used="read_file",
            outcome="permission denied",
        ))
        results = tmp_loop.get_relevant_lessons("call web_search for tokyo weather", top_k=3)
        assert len(results) >= 1
        # 最相关的应该提到 web_search
        assert "web_search" in results[0].content.lower()

    def test_empty_query_returns_empty(self, tmp_loop):
        tmp_loop.record(LearningEvent(
            event_type=EventType.SUCCESS,
            task_description="some task",
            approach_used="x",
            outcome="ok",
        ))
        assert tmp_loop.get_relevant_lessons("") == []
        assert tmp_loop.get_relevant_lessons("   ") == []

    def test_top_k_limit(self, tmp_loop):
        for i in range(5):
            tmp_loop.record(LearningEvent(
                event_type=EventType.SUCCESS,
                task_description=f"web_search task number {i}",
                approach_used="web_search",
                outcome=f"ok {i}",
            ))
        results = tmp_loop.get_relevant_lessons("web_search", top_k=2)
        assert len(results) <= 2


# ============================================================
# test_merge_similar_lessons
# ============================================================

def test_merge_similar_lessons(tmp_loop):
    # 两条高度相似的教训 (仅末尾不同)
    base = "Approach 'web_search' failed for tasks like 'query tokyo weather'. Reason: timeout after 15s. Avoid repeating this approach."
    similar = "Approach 'web_search' failed for tasks like 'query tokyo weather'. Reason: timeout after 20s. Avoid repeating this approach."
    e1 = LearningEvent(
        event_type=EventType.FAILURE,
        task_description="query tokyo weather",
        approach_used="web_search",
        outcome="timeout after 15s",
        lessons=[base],
    )
    e2 = LearningEvent(
        event_type=EventType.FAILURE,
        task_description="query tokyo weather",
        approach_used="web_search",
        outcome="timeout after 20s",
        lessons=[similar],
    )
    tmp_loop.record(e1)
    tmp_loop.record(e2)
    # 相似度 > 0.8, 应合并为一条
    all_lessons = tmp_loop.get_all_lessons()
    assert len(all_lessons) == 1
    # confidence 应增加
    assert all_lessons[0].confidence > 1.0
    assert all_lessons[0].occurrence_count == 2


# ============================================================
# test_persist_load
# ============================================================

def test_persist_load(tmp_path):
    path = tmp_path / "learning_feedback.json"
    loop1 = LearningFeedbackLoop(persist_path=path)
    loop1.record(LearningEvent(
        event_type=EventType.SUCCESS,
        task_description="test task",
        approach_used="approach_a",
        outcome="ok",
    ))
    loop1.update_strategy("task_pattern_x", "use approach_a")
    loop1.persist()
    assert path.exists()

    # 新实例从同一文件加载
    loop2 = LearningFeedbackLoop(persist_path=path)
    lessons = loop2.get_all_lessons()
    assert len(lessons) == 1
    assert "test task" in lessons[0].content
    # 策略也应被加载
    assert loop2.get_strategy("task_pattern_x") == "use approach_a"


def test_persist_creates_valid_json(tmp_path):
    path = tmp_path / "learning_feedback.json"
    loop = LearningFeedbackLoop(persist_path=path)
    loop.record(LearningEvent(
        event_type=EventType.FAILURE,
        task_description="task",
        approach_used="x",
        outcome="fail",
    ))
    loop.persist()
    # 验证文件是合法 JSON
    import json
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "lessons" in data
    assert "strategies" in data
    assert len(data["lessons"]) == 1


# ============================================================
# test_update_strategy
# ============================================================

class TestUpdateStrategy:
    def test_update_and_get(self, tmp_loop):
        tmp_loop.update_strategy("web_search_timeout", "increase timeout to 30s")
        assert tmp_loop.get_strategy("web_search_timeout") == "increase timeout to 30s"

    def test_get_nonexistent(self, tmp_loop):
        assert tmp_loop.get_strategy("nonexistent") is None

    def test_get_empty(self, tmp_loop):
        assert tmp_loop.get_strategy("") is None

    def test_substring_match(self, tmp_loop):
        tmp_loop.update_strategy("web_search timeout", "increase timeout")
        # 用 key 的子串查询
        assert tmp_loop.get_strategy("timeout") == "increase timeout"
        # 用包含 key 的字符串查询
        assert tmp_loop.get_strategy("handle web_search timeout gracefully") == "increase timeout"

    def test_overwrite(self, tmp_loop):
        tmp_loop.update_strategy("p", "v1")
        tmp_loop.update_strategy("p", "v2")
        assert tmp_loop.get_strategy("p") == "v2"


# ============================================================
# test_confidence_increases
# ============================================================

def test_confidence_increases(tmp_loop):
    # 同一条教训重复记录多次, confidence 应单调递增
    base_event = LearningEvent(
        event_type=EventType.FAILURE,
        task_description="call web_search",
        approach_used="web_search",
        outcome="timeout",
        lessons=["web_search times out for slow queries. Avoid web_search for slow queries."],
    )
    tmp_loop.record(base_event)
    after_1 = tmp_loop.get_all_lessons()[0]
    conf_1 = after_1.confidence
    occ_1 = after_1.occurrence_count

    tmp_loop.record(base_event)
    after_2 = tmp_loop.get_all_lessons()[0]
    conf_2 = after_2.confidence
    occ_2 = after_2.occurrence_count

    assert conf_2 > conf_1
    assert occ_2 == occ_1 + 1
    # 不应超过上限
    assert conf_2 <= 10.0

    # 重复多次验证上限
    for _ in range(15):
        tmp_loop.record(base_event)
    final = tmp_loop.get_all_lessons()[0]
    assert final.confidence == 10.0
    assert final.occurrence_count == 17


# ============================================================
# 集成: 工具执行结果记录
# ============================================================

class TestIntegrationHelpers:
    def test_record_tool_outcome_success(self, monkeypatch, tmp_path):
        # 重置单例并指向临时路径
        import core.learning_feedback as mod
        monkeypatch.setattr(mod, "_singleton", None)
        loop = LearningFeedbackLoop(persist_path=tmp_path / "lf.json")
        monkeypatch.setattr(mod, "_singleton", loop)

        record_tool_outcome(
            tool_name="web_search",
            arguments={"query": "tokyo weather"},
            success=True,
            duration=0.5,
        )
        assert len(loop.get_all_lessons()) >= 1

    def test_record_tool_outcome_failure(self, monkeypatch, tmp_path):
        import core.learning_feedback as mod
        monkeypatch.setattr(mod, "_singleton", None)
        loop = LearningFeedbackLoop(persist_path=tmp_path / "lf.json")
        monkeypatch.setattr(mod, "_singleton", loop)

        record_tool_outcome(
            tool_name="shell_command",
            arguments={"cmd": "ls /nonexistent"},
            success=False,
            error="no such directory",
        )
        lessons = loop.get_all_lessons()
        assert len(lessons) >= 1
        assert any(l.event_type == EventType.FAILURE for l in lessons)

    def test_record_reflection_lesson(self, monkeypatch, tmp_path):
        import core.learning_feedback as mod
        monkeypatch.setattr(mod, "_singleton", None)
        loop = LearningFeedbackLoop(persist_path=tmp_path / "lf.json")
        monkeypatch.setattr(mod, "_singleton", loop)

        record_reflection_lesson(
            lesson_text="Verify resource exists before calling web_search.",
            pattern="resource_not_found",
            correction="Check existence with a probe call first",
        )
        lessons = loop.get_all_lessons()
        assert len(lessons) >= 1
        # 教训文本应包含 correction
        assert any("probe call" in l.content for l in lessons)


# ============================================================
# 集成: Agent-R 反思器 → 学习闭环
# ============================================================

def test_agent_r_reflector_integration(monkeypatch, tmp_path):
    """Agent-R reflect 生成教训后, 通过 record_reflection_lesson 注入学习闭环"""
    import core.learning_feedback as mod
    from core.agent_r_reflection import AgentRReflector

    monkeypatch.setattr(mod, "_singleton", None)
    loop = LearningFeedbackLoop(persist_path=tmp_path / "lf.json")
    monkeypatch.setattr(mod, "_singleton", loop)

    reflector = AgentRReflector()
    reflector.record_step("tool_call", "fail", success=False, error="timeout")
    memory = reflector.reflect()
    assert memory is not None

    # 调用方 (不修改 AgentRReflector) 主动注入到学习闭环
    record_reflection_lesson(
        lesson_text=memory.lesson,
        pattern=memory.pattern,
        correction=memory.correction,
    )

    # 学习闭环应有相应教训
    lessons = loop.get_all_lessons()
    assert len(lessons) >= 1
    assert any("timeout" in l.content.lower() for l in lessons)


# ============================================================
# 边界: 加载损坏文件
# ============================================================

def test_load_corrupted_file(tmp_path):
    path = tmp_path / "learning_feedback.json"
    path.write_text("not a valid json {{{", encoding="utf-8")
    # 不应抛异常, 退化为空状态
    loop = LearningFeedbackLoop(persist_path=path)
    assert loop.get_all_lessons() == []
    assert loop.get_all_strategies() == {}


def test_load_missing_file(tmp_path):
    path = tmp_path / "nonexistent.json"
    loop = LearningFeedbackLoop(persist_path=path)
    assert loop.get_all_lessons() == []
    assert loop.get_all_strategies() == {}


# ============================================================
# 统计
# ============================================================

def test_stats(tmp_loop):
    tmp_loop.record(LearningEvent(
        event_type=EventType.SUCCESS,
        task_description="t1",
        approach_used="a",
        outcome="ok",
    ))
    tmp_loop.record(LearningEvent(
        event_type=EventType.FAILURE,
        task_description="t2",
        approach_used="b",
        outcome="fail",
    ))
    tmp_loop.update_strategy("p1", "s1")
    stats = tmp_loop.get_stats()
    assert stats["total_lessons"] == 2
    assert stats["total_strategies"] == 1
    assert stats["by_event_type"].get("success") == 1
    assert stats["by_event_type"].get("failure") == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
