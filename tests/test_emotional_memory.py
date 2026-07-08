"""情感记忆系统单元测试 — Stanislavski 情感记忆理论

覆盖 4 阶段能力：
- Anchoring：存储 + 自动关键词提取
- Recalling：Jaccard 相似度召回 + 无匹配返回空
- Bounding：注入上限 + 不重复注入 + 会话重置
- Enacting：小妲口吻复述 + XP 等级影响口吻
- 集成入口 recall_and_enact + 环境变量门控 + 持久化往返
"""

from memory.emotional_memory import EmotionalMemory, EmotionalMemoryManager


# ── Anchoring ──


def test_anchor_store(tmp_path):
    """Anchoring：存储情感记忆并落盘"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mem = mgr.anchor(
        user_id="u1",
        event="用户提到工作压力大",
        emotion="焦虑",
        context="最近工作压力太大了，天天加班",
        keywords=["工作", "压力"],
    )
    assert mem.user_id == "u1"
    assert mem.event == "用户提到工作压力大"
    assert mem.emotion == "焦虑"
    assert mem.keywords == ["工作", "压力"]
    assert mem.id.startswith("em_")
    assert mem.timestamp > 0
    # 已存入内存
    assert len(mgr._memories["u1"]) == 1
    assert mgr._memories["u1"][0].id == mem.id
    # 已落盘
    assert mgr._memories_path.exists()


def test_anchor_with_auto_keywords(tmp_path):
    """Anchoring：keywords=None 时自动提取关键词"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mem = mgr.anchor(
        user_id="u1",
        event="用户很开心",
        emotion="喜悦",
        context="加班 加薪 升职",
        keywords=None,  # 自动提取
    )
    # 自动提取应包含非停用词、长度>=2 的分词
    assert mem.keywords, "自动提取的关键词不应为空"
    assert "升职" in mem.keywords
    assert "加班" in mem.keywords
    # 停用词应被剔除
    assert "的" not in mem.keywords


# ── Recalling ──


def test_recall_jaccard(tmp_path):
    """Recalling：Jaccard 相似度召回相关记忆"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mgr.anchor("u1", "工作压力", "焦虑", "工作压力很大", keywords=["工作", "压力"])
    mgr.anchor("u1", "周末计划", "开心", "周末出去玩", keywords=["周末", "出去玩"])

    # 查询与第一条关键词完全重叠
    results = mgr.recall("u1", "工作 压力")
    assert len(results) == 1
    assert results[0].event == "工作压力"
    assert results[0].emotion == "焦虑"
    # 召回计数与时间戳已更新
    assert results[0].recall_count == 1
    assert results[0].last_recalled_at > 0


def test_recall_no_match(tmp_path):
    """Recalling：无关键词重叠时返回空"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mgr.anchor("u1", "工作压力", "焦虑", "工作压力很大", keywords=["工作", "压力"])

    results = mgr.recall("u1", "周末 出去玩")
    assert results == []


def test_recall_unknown_user(tmp_path):
    """Recalling：未知用户返回空"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    assert mgr.recall("unknown", "任意查询") == []


# ── Bounding ──


def test_bound_limit(tmp_path):
    """Bounding：单次会话注入上限为 MAX_INJECT_PER_SESSION"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    # 锚定 5 条都匹配同一查询的记忆
    for i in range(5):
        mgr.anchor("u1", f"事件{i}", "焦虑", f"工作压力{i}", keywords=["工作", "压力"])
    recalled = mgr.recall("u1", "工作 压力", top_k=5)
    assert len(recalled) == 5
    bounded = mgr.bound("u1", recalled)
    # 最多 MAX_INJECT_PER_SESSION = 3
    assert len(bounded) == EmotionalMemoryManager.MAX_INJECT_PER_SESSION


def test_bound_no_duplicate(tmp_path):
    """Bounding：已注入的记忆不重复注入；reset_session 后可再次注入"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mgr.anchor("u1", "事件A", "焦虑", "工作压力", keywords=["工作", "压力"])
    recalled = mgr.recall("u1", "工作 压力")

    first = mgr.bound("u1", recalled)
    assert len(first) == 1
    # 再次 bound 同样的记忆 → 不重复注入
    second = mgr.bound("u1", recalled)
    assert second == []
    # reset 后可再次注入
    mgr.reset_session("u1")
    third = mgr.bound("u1", recalled)
    assert len(third) == 1


# ── Enacting ──


def test_enact_format(tmp_path):
    """Enacting：输出格式正确"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mem = EmotionalMemory(
        id="em_1",
        user_id="u1",
        event="用户提到工作压力大",
        emotion="焦虑",
        context="最近工作压力太大了",
        keywords=["工作", "压力"],
    )
    out = mgr.enact([mem], user_xp_level=1)
    assert out.startswith("[情感记忆召回]")
    assert "用户提到工作压力大" in out
    assert "焦虑" in out
    assert "我想到" in out  # 低等级 opener
    assert out.endswith("(请在回复中自然地提及这些记忆，避免生硬)")


def test_enact_empty(tmp_path):
    """Enacting：空列表返回空串"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    assert mgr.enact([], user_xp_level=1) == ""


def test_enact_xp_level_affects_tone(tmp_path):
    """Enacting：XP 等级影响口吻亲密度"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mem = EmotionalMemory(
        id="em_1",
        user_id="u1",
        event="升职庆祝",
        emotion="喜悦",
        context="今天升职了非常开心",
        keywords=["升职"],
    )
    low = mgr.enact([mem], user_xp_level=1)
    high = mgr.enact([mem], user_xp_level=3)
    # 低等级用 "我想到"，不含原话引用
    assert "我想到" in low
    assert "记得" not in low
    assert "你说过" not in low
    # 高等级用 "记得"，并引用原话
    assert "记得" in high
    assert "今天升职了非常开心" in high


# ── 集成入口 ──


def test_recall_and_enact_integration(tmp_path, monkeypatch):
    """集成：recall + bound + enact 一步到位"""
    monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mgr.anchor("u1", "工作压力", "焦虑", "工作压力很大", keywords=["工作", "压力"])
    out = mgr.recall_and_enact("u1", "工作 压力", user_xp_level=2)
    assert out.startswith("[情感记忆召回]")
    assert "工作压力" in out


def test_env_var_disables_injection(tmp_path, monkeypatch):
    """环境变量 EMOTIONAL_MEMORY_ENABLED=0 时不注入记忆"""
    monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "0")
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    mgr.anchor("u1", "工作压力", "焦虑", "工作压力很大", keywords=["工作", "压力"])
    # 关闭后不注入任何记忆
    assert mgr.recall_and_enact("u1", "工作 压力") == ""


# ── 持久化 ──


def test_persistence(tmp_path):
    """持久化：保存后由新 manager 加载，字段一致"""
    mgr1 = EmotionalMemoryManager(data_dir=tmp_path)
    mem = mgr1.anchor("u1", "工作压力", "焦虑", "工作压力很大", keywords=["工作", "压力"])
    mem.recall_count = 2
    mgr1._save()

    # 新建 manager 加载同一目录
    mgr2 = EmotionalMemoryManager(data_dir=tmp_path)
    assert "u1" in mgr2._memories
    assert len(mgr2._memories["u1"]) == 1
    loaded = mgr2._memories["u1"][0]
    assert loaded.id == mem.id
    assert loaded.event == "工作压力"
    assert loaded.emotion == "焦虑"
    assert loaded.keywords == ["工作", "压力"]
    assert loaded.recall_count == 2


def test_persistence_empty_dir(tmp_path):
    """持久化：目录无文件时正常初始化为空"""
    mgr = EmotionalMemoryManager(data_dir=tmp_path)
    assert mgr._memories == {}
    assert mgr.recall("u1", "任意") == []
