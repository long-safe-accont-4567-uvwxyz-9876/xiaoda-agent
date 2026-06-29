"""永久记忆 (PermanentMemoryManager) 单元测试

覆盖:
- test_store_and_retrieve: 存储和获取
- test_record_key_event_no_duplicate: 关键事件不重复记录
- test_set_and_get_preference: 偏好设置和获取
- test_record_milestone: 里程碑记录
- test_session_opener_long_absence: 长时间未互动开场白
- test_session_opener_short_absence: 短时间无特殊开场
- test_prompt_segment_format: prompt 段落格式
- test_persistence: 保存/加载往返
"""
import json
import time
from pathlib import Path

import pytest

# 确保项目根在 path (与 conftest.py 对齐)
ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _ensure_pm_enabled(monkeypatch):
    """每个测试默认启用永久记忆."""
    monkeypatch.setenv("PERMANENT_MEMORY_ENABLED", "true")


@pytest.fixture
def tmp_pm(tmp_path):
    """每个测试用独立的临时数据目录, 避免污染单例."""
    import core.permanent_memory as mod
    return mod.PermanentMemoryManager(data_dir=tmp_path)


# ============================================================
# test_store_and_retrieve
# ============================================================

def test_store_and_retrieve(tmp_pm):
    """存储和获取: 基本 CRUD 流程."""
    entry = tmp_pm.store(
        user_id="u1",
        category="key_event",
        key="first_chat",
        value="首次对话发生",
        source="system",
        metadata={"channel": "web"},
    )
    assert entry.id == "u1:first_chat"
    assert entry.user_id == "u1"
    assert entry.category == "key_event"
    assert entry.key == "first_chat"
    assert entry.value == "首次对话发生"
    assert entry.source == "system"
    assert entry.metadata == {"channel": "web"}
    assert entry.timestamp > 0

    # retrieve 返回同一对象
    got = tmp_pm.retrieve("u1", "first_chat")
    assert got is not None
    assert got.value == "首次对话发生"

    # 不存在的 key 返回 None
    assert tmp_pm.retrieve("u1", "nope") is None
    assert tmp_pm.retrieve("u_unknown", "first_chat") is None


def test_store_update_keeps_timestamp(tmp_pm):
    """重复 store 同一 key: 更新 value, 保留原 timestamp."""
    entry1 = tmp_pm.store("u1", "preference", "preferred_name", "小纳")
    original_ts = entry1.timestamp
    # 稍等以保证 time.time() 不同
    time.sleep(0.01)

    entry2 = tmp_pm.store("u1", "preference", "preferred_name", "纳纳",
                          source="user")
    assert entry2.value == "纳纳"
    assert entry2.source == "user"
    # timestamp 应保留首次创建时的时间
    assert entry2.timestamp == original_ts


def test_retrieve_by_category(tmp_pm):
    """按类别检索记忆."""
    tmp_pm.store("u1", "preference", "name", "小纳")
    tmp_pm.store("u1", "preference", "tone", "温柔")
    tmp_pm.store("u1", "key_event", "first_chat", "首次对话")
    tmp_pm.store("u1", "relationship", "milestone_1", "成为朋友")

    prefs = tmp_pm.retrieve_by_category("u1", "preference")
    assert len(prefs) == 2
    keys = {p.key for p in prefs}
    assert keys == {"name", "tone"}

    events = tmp_pm.retrieve_by_category("u1", "key_event")
    assert len(events) == 1
    assert events[0].key == "first_chat"


def test_delete(tmp_pm):
    """删除记忆."""
    tmp_pm.store("u1", "preference", "name", "小纳")
    assert tmp_pm.retrieve("u1", "name") is not None

    deleted = tmp_pm.delete("u1", "name")
    assert deleted is True
    assert tmp_pm.retrieve("u1", "name") is None

    # 再次删除返回 False
    assert tmp_pm.delete("u1", "name") is False


# ============================================================
# test_record_key_event_no_duplicate
# ============================================================

def test_record_key_event_no_duplicate(tmp_pm):
    """关键事件不重复记录: 第二次调用返回已存在的条目."""
    first = tmp_pm.record_key_event(
        "u1", "first_chat", "首次对话发生",
        metadata={"channel": "web"},
    )
    assert first is not None
    assert first.key == "first_chat"
    assert first.category == "key_event"
    assert first.value == "首次对话发生"
    assert first.metadata == {"channel": "web"}

    # 再次记录同一事件类型: 不应重复
    second = tmp_pm.record_key_event("u1", "first_chat", "再次首次对话")
    assert second is first  # 返回已存在的同一对象
    # value 不被覆盖
    assert second.value == "首次对话发生"

    # 用户只有一条记忆
    all_mem = tmp_pm.retrieve_all("u1")
    assert len(all_mem) == 1


def test_record_key_event_unknown_type(tmp_pm):
    """未知事件类型返回 None, 不存储."""
    result = tmp_pm.record_key_event("u1", "unknown_event", "未知事件")
    assert result is None
    assert tmp_pm.retrieve_all("u1") == {}


def test_record_key_event_all_types(tmp_pm):
    """所有 KEY_EVENTS 类型均可被记录."""
    for event_type, label in tmp_pm.KEY_EVENTS.items():
        entry = tmp_pm.record_key_event("u1", event_type, f"{label}描述")
        assert entry is not None
        assert entry.key == event_type
    # 每个类型一条
    assert len(tmp_pm.retrieve_all("u1")) == len(tmp_pm.KEY_EVENTS)


# ============================================================
# test_set_and_get_preference
# ============================================================

def test_set_and_get_preference(tmp_pm):
    """偏好设置和获取."""
    # 不存在时返回 None
    assert tmp_pm.get_preference("u1", "preferred_name") is None

    entry = tmp_pm.set_preference("u1", "preferred_name", "小纳")
    assert entry.category == "preference"
    assert entry.source == "agent"  # 偏好默认来源 agent
    assert entry.value == "小纳"

    # 获取
    assert tmp_pm.get_preference("u1", "preferred_name") == "小纳"

    # 更新偏好
    tmp_pm.set_preference("u1", "preferred_name", "纳纳")
    assert tmp_pm.get_preference("u1", "preferred_name") == "纳纳"


# ============================================================
# test_record_milestone
# ============================================================

def test_record_milestone(tmp_pm):
    """里程碑记录: category=relationship, key 带 milestone_ 前缀."""
    entry = tmp_pm.record_milestone(
        "u1", "成为朋友", metadata={"level": 3},
    )
    assert entry.category == "relationship"
    assert entry.key.startswith("milestone_")
    assert entry.value == "成为朋友"
    assert entry.source == "system"
    assert entry.metadata == {"level": 3}
    assert entry.timestamp > 0

    # retrieve_by_category 可查到
    milestones = tmp_pm.retrieve_by_category("u1", "relationship")
    assert len(milestones) == 1
    assert milestones[0].value == "成为朋友"


# ============================================================
# test_session_opener_long_absence
# ============================================================

def test_session_opener_long_absence(tmp_pm):
    """长时间未互动 (>=7 天) 开场白含 '好久不见' 并提及最近里程碑."""
    # 先记录首次对话 + 一条里程碑
    tmp_pm.record_key_event("u1", "first_chat", "首次对话")
    tmp_pm.record_milestone("u1", "成为朋友")

    # 把所有记忆的 timestamp 回拨到 10 天前, 模拟长时间未互动
    ten_days_ago = time.time() - 10 * 86400
    for entry in tmp_pm.retrieve_all("u1").values():
        entry.timestamp = ten_days_ago
    tmp_pm._save()

    opener = tmp_pm.get_session_opener("u1")
    assert "好久不见" in opener
    assert "10" in opener  # 天数
    # 提及最近里程碑
    assert "成为朋友" in opener


def test_session_opener_no_milestone_long_absence(tmp_pm):
    """长时间未互动但无里程碑: 开场白仍含 '好久不见', 不附加里程碑行."""
    tmp_pm.record_key_event("u1", "first_chat", "首次对话")
    ten_days_ago = time.time() - 8 * 86400
    for entry in tmp_pm.retrieve_all("u1").values():
        entry.timestamp = ten_days_ago
    tmp_pm._save()

    opener = tmp_pm.get_session_opener("u1")
    assert "好久不见" in opener
    assert "记得上次我们聊到" not in opener


# ============================================================
# test_session_opener_short_absence
# ============================================================

def test_session_opener_short_absence(tmp_pm):
    """短时间 (<1 天) 重启: 无特殊开场 (返回空串)."""
    tmp_pm.record_key_event("u1", "first_chat", "首次对话")
    # 不修改 timestamp, 当前时间即最近互动
    opener = tmp_pm.get_session_opener("u1")
    assert opener == ""


def test_session_opener_medium_absence(tmp_pm):
    """1-7 天内重启: 返回 '又见面啦' 开场白."""
    tmp_pm.record_key_event("u1", "first_chat", "首次对话")
    two_days_ago = time.time() - 2 * 86400
    for entry in tmp_pm.retrieve_all("u1").values():
        entry.timestamp = two_days_ago
    tmp_pm._save()

    opener = tmp_pm.get_session_opener("u1")
    assert "又见面啦" in opener


def test_session_opener_new_user(tmp_pm):
    """新用户 (无任何记忆) 开场白为空."""
    assert tmp_pm.get_session_opener("u_new") == ""


def test_session_opener_no_first_chat(tmp_pm):
    """有记忆但无 first_chat: 开场白为空."""
    tmp_pm.set_preference("u1", "name", "小纳")
    assert tmp_pm.get_session_opener("u1") == ""


# ============================================================
# test_prompt_segment_format
# ============================================================

def test_prompt_segment_format(tmp_pm):
    """prompt 段落格式: 包含偏好与关键事件."""
    tmp_pm.set_preference("u1", "preferred_name", "小纳")
    tmp_pm.set_preference("u1", "tone", "温柔")
    tmp_pm.record_key_event("u1", "first_chat", "首次对话")
    tmp_pm.record_key_event("u1", "first_deep_chat", "首次深度对话")

    segment = tmp_pm.get_prompt_segment("u1")
    assert "[永久记忆]" in segment
    assert "用户偏好" in segment
    assert "preferred_name: 小纳" in segment
    assert "tone: 温柔" in segment
    assert "近期关键事件" in segment
    assert "首次对话" in segment
    assert "首次深度对话" in segment


def test_prompt_segment_empty(tmp_pm):
    """无记忆时 prompt 段落为空."""
    assert tmp_pm.get_prompt_segment("u_new") == ""


def test_prompt_segment_only_preferences(tmp_pm):
    """只有偏好时: prompt 段落含偏好但无关键事件行."""
    tmp_pm.set_preference("u1", "name", "小纳")
    segment = tmp_pm.get_prompt_segment("u1")
    assert "[永久记忆]" in segment
    assert "name: 小纳" in segment
    assert "近期关键事件" not in segment


def test_prompt_segment_key_events_limit(tmp_pm):
    """prompt 段落最多展示最近 3 个关键事件."""
    for i in range(5):
        # 使用不同 key 避免合并, 时间递增保证排序稳定
        entry = tmp_pm.store("u1", "key_event", f"evt_{i}",
                              f"事件_{i}")
        entry.timestamp = 1000.0 + i  # 强制递增
    tmp_pm._save()

    segment = tmp_pm.get_prompt_segment("u1")
    # 最新 3 个: 事件_2, 事件_3, 事件_4
    assert "事件_4" in segment
    assert "事件_3" in segment
    assert "事件_2" in segment
    # 旧的两个不应出现
    assert "事件_0" not in segment
    assert "事件_1" not in segment


# ============================================================
# test_persistence
# ============================================================

def test_persistence(tmp_path):
    """保存/加载往返: 数据不丢失."""
    import core.permanent_memory as mod

    sys1 = mod.PermanentMemoryManager(data_dir=tmp_path)
    sys1.record_key_event("u1", "first_chat", "首次对话",
                           metadata={"channel": "web"})
    sys1.set_preference("u1", "preferred_name", "小纳")
    sys1.record_milestone("u1", "成为朋友")

    # 持久化文件已生成
    assert sys1._memories_path.exists()

    # 新实例从同一文件加载
    sys2 = mod.PermanentMemoryManager(data_dir=tmp_path)
    # key_event
    evt = sys2.retrieve("u1", "first_chat")
    assert evt is not None
    assert evt.value == "首次对话"
    assert evt.category == "key_event"
    assert evt.metadata == {"channel": "web"}
    # preference
    assert sys2.get_preference("u1", "preferred_name") == "小纳"
    # milestone
    milestones = sys2.retrieve_by_category("u1", "relationship")
    assert len(milestones) == 1
    assert milestones[0].value == "成为朋友"


def test_persistence_valid_json(tmp_path):
    """持久化文件应是合法 JSON 且结构完整: {user_id: {key: entry_dict}}."""
    import core.permanent_memory as mod
    sys1 = mod.PermanentMemoryManager(data_dir=tmp_path)
    sys1.set_preference("u1", "name", "小纳")
    sys1._save()

    with open(sys1._memories_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert "u1" in raw
    assert "name" in raw["u1"]
    entry_dict = raw["u1"]["name"]
    assert entry_dict["user_id"] == "u1"
    assert entry_dict["category"] == "preference"
    assert entry_dict["key"] == "name"
    assert entry_dict["value"] == "小纳"
    assert entry_dict["id"] == "u1:name"


def test_load_corrupted_file(tmp_path):
    """加载损坏文件时应回退到空状态, 不抛异常."""
    import core.permanent_memory as mod
    path = tmp_path / "permanent_memories.json"
    path.write_text("not a valid json {{{", encoding="utf-8")

    sys_inst = mod.PermanentMemoryManager(data_dir=tmp_path)
    # 应不抛异常, 状态为空
    assert sys_inst.retrieve_all("u1") == {}


def test_load_nonexistent_file(tmp_path):
    """加载不存在的文件时保持空状态."""
    import core.permanent_memory as mod
    sys_inst = mod.PermanentMemoryManager(data_dir=tmp_path)
    assert sys_inst.retrieve_all("u1") == {}


# ============================================================
# 环境变量开关
# ============================================================

def test_env_var_disable(tmp_path, monkeypatch):
    """PERMANENT_MEMORY_ENABLED=0 时功能关闭, 不持久化."""
    monkeypatch.setenv("PERMANENT_MEMORY_ENABLED", "0")
    import core.permanent_memory as mod
    mgr = mod.PermanentMemoryManager(data_dir=tmp_path)
    assert mgr.enabled is False

    # store 不应持久化 (返回临时对象, 不写入文件)
    entry = mgr.store("u1", "preference", "name", "小纳")
    assert entry.value == "小纳"
    # 文件不应被创建
    assert not mgr._memories_path.exists()

    # 开场白与 prompt 段落为空
    assert mgr.get_session_opener("u1") == ""
    assert mgr.get_prompt_segment("u1") == ""


def test_env_var_enable_default(tmp_path, monkeypatch):
    """未设置环境变量时默认开启."""
    monkeypatch.delenv("PERMANENT_MEMORY_ENABLED", raising=False)
    import core.permanent_memory as mod
    mgr = mod.PermanentMemoryManager(data_dir=tmp_path)
    assert mgr.enabled is True


# ============================================================
# 单例
# ============================================================

def test_singleton():
    """get_permanent_memory_manager 返回全局单例."""
    import core.permanent_memory as mod
    mod.reset_permanent_memory_manager()
    a = mod.get_permanent_memory_manager()
    b = mod.get_permanent_memory_manager()
    assert a is b
    mod.reset_permanent_memory_manager()


def test_reset_singleton():
    """reset 后再获取应是新实例."""
    import core.permanent_memory as mod
    mod.reset_permanent_memory_manager()
    a = mod.get_permanent_memory_manager()
    mod.reset_permanent_memory_manager()
    b = mod.get_permanent_memory_manager()
    assert a is not b
    mod.reset_permanent_memory_manager()


# ============================================================
# from_dict / to_dict 往返
# ============================================================

def test_entry_dict_roundtrip():
    """PermanentMemoryEntry 序列化往返."""
    import core.permanent_memory as mod
    entry = mod.PermanentMemoryEntry(
        id="u1:first_chat",
        user_id="u1",
        category="key_event",
        key="first_chat",
        value="首次对话",
        timestamp=1234567890.0,
        source="system",
        metadata={"channel": "web"},
    )
    d = entry.to_dict()
    restored = mod.PermanentMemoryEntry.from_dict(d)
    assert restored.id == entry.id
    assert restored.user_id == entry.user_id
    assert restored.category == entry.category
    assert restored.key == entry.key
    assert restored.value == entry.value
    assert restored.timestamp == entry.timestamp
    assert restored.source == entry.source
    assert restored.metadata == entry.metadata


def test_entry_from_dict_missing_fields():
    """from_dict 对缺失字段使用默认值, 不抛异常."""
    import core.permanent_memory as mod
    entry = mod.PermanentMemoryEntry.from_dict({"key": "k", "value": "v"})
    assert entry.key == "k"
    assert entry.value == "v"
    assert entry.id == ""
    assert entry.user_id == ""
    assert entry.category == ""
    assert entry.source == "system"
    assert entry.metadata == {}
    assert entry.timestamp == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
