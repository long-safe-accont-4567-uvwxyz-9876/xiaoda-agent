"""L/M/S 三层心理状态模型单元测试

参考: ACL 2026 Dynamic Persona Coherence

测试覆盖:
- 三层状态初始化
- 保存/加载往返
- L 层从 SOUL.md/IDENTITY.md 加载
- S 层实时更新
- M 层添加主题/压力事件/里程碑
- Dream 时清理 7 天前数据
- prompt 段落格式
- 环境变量开关
"""
import json
import time

from core.mental_state import (
    LongTermIdentity,
    MediumTermState,
    MentalState,
    MentalStateManager,
    ShortTermEmotion,
    get_mental_state_manager,
    reset_mental_state_manager,
)

# ── 初始化 ──────────────────────────────────────────────


def test_mental_state_init():
    """初始化三层状态: L/M/S 均为默认空值"""
    ms = MentalState()
    assert isinstance(ms.L, LongTermIdentity)
    assert isinstance(ms.M, MediumTermState)
    assert isinstance(ms.S, ShortTermEmotion)
    assert ms.L.soul_content == ""
    assert ms.L.identity_content == ""
    assert ms.L.core_traits == []
    assert ms.L.last_updated == 0.0
    assert ms.M.recent_themes == []
    assert ms.M.stress_events == []
    assert ms.M.relationship_milestones == []
    assert ms.M.last_dream_at == 0.0
    assert ms.S.current_emotion == ""
    assert ms.S.user_last_emotion == ""
    assert ms.S.emotion_history == []


# ── 保存/加载往返 ──────────────────────────────────────


def test_load_save_roundtrip(tmp_path):
    """保存/加载往返: 数据不丢失"""
    ms = MentalState()
    ms.L.soul_content = "小妲的灵魂"
    ms.L.identity_content = "小妲的身份"
    ms.L.core_traits = ["温柔", "聪慧", "耐心"]
    ms.L.last_updated = 1000.0
    ms.M.recent_themes = ["工作", "编程"]
    ms.M.stress_events = [{"event": "deadline", "ts": time.time()}]
    ms.M.relationship_milestones = [{"event": "首次对话", "ts": time.time()}]
    ms.S.current_emotion = "喜悦"
    ms.S.user_last_emotion = "焦虑"
    ms.S.emotion_history = [{"ts": time.time(), "emotion": "开心"}]

    path = tmp_path / "mental_state.json"
    ms.save(path)

    # 文件确实写入了
    assert path.exists()
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert "L" in raw and "M" in raw and "S" in raw

    # 往返加载
    loaded = MentalState.load(path)
    assert loaded.L.soul_content == "小妲的灵魂"
    assert loaded.L.identity_content == "小妲的身份"
    assert loaded.L.core_traits == ["温柔", "聪慧", "耐心"]
    assert loaded.L.last_updated == 1000.0
    assert loaded.M.recent_themes == ["工作", "编程"]
    assert len(loaded.M.stress_events) == 1
    assert loaded.M.stress_events[0]["event"] == "deadline"
    assert len(loaded.M.relationship_milestones) == 1
    assert loaded.S.current_emotion == "喜悦"
    assert loaded.S.user_last_emotion == "焦虑"
    assert len(loaded.S.emotion_history) == 1


def test_load_nonexistent_returns_empty(tmp_path):
    """加载不存在的文件返回空状态"""
    ms = MentalState.load(tmp_path / "nope.json")
    assert ms.L.soul_content == ""
    assert ms.M.recent_themes == []


# ── L 层: 从文件加载 ───────────────────────────────────


def test_l_layer_load_from_files(tmp_path):
    """从 SOUL.md/IDENTITY.md 加载 L 层, 提取核心特质"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text(
        "# SOUL.md\n\n你是小妲。\n\n## 核心人格\n\n- 温柔、聪慧、耐心、认真\n- 说话轻柔清脆\n",
        encoding="utf-8",
    )
    (workspace / "IDENTITY.md").write_text(
        "# IDENTITY.md\n\n你是 AI 编程助手，名字是小妲。\n",
        encoding="utf-8",
    )

    mgr = MentalStateManager(data_dir=tmp_path)
    mgr.reload_long_term(workspace)

    assert "小妲" in mgr.state.L.soul_content
    assert "AI 编程助手" in mgr.state.L.identity_content
    # 核心特质从第一个列表项提取
    assert mgr.state.L.core_traits == ["温柔", "聪慧", "耐心", "认真"]
    assert mgr.state.L.last_updated > 0
    # G3: _save 为 debounce，需 flush 立即写盘
    mgr.flush()
    # 持久化文件已生成
    assert (tmp_path / "mental_state.json").exists()


def test_l_layer_fallback_to_tpl(tmp_path):
    """SOUL.md 不存在时回退到 SOUL.md.tpl"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SOUL.md.tpl").write_text(
        "## 核心人格\n\n- 温柔、聪慧\n", encoding="utf-8",
    )
    mgr = MentalStateManager(data_dir=tmp_path)
    mgr.reload_long_term(workspace)
    assert mgr.state.L.core_traits == ["温柔", "聪慧"]


# ── S 层: 实时更新 ─────────────────────────────────────


def test_s_layer_update_realtime(tmp_path):
    """S 层实时更新: 情绪与历史记录"""
    mgr = MentalStateManager(data_dir=tmp_path)
    assert mgr.state.S.current_emotion == ""

    mgr.update_short_term(emotion="开心", user_emotion="喜悦")
    assert mgr.state.S.current_emotion == "开心"
    assert mgr.state.S.user_last_emotion == "喜悦"
    assert mgr.state.S.session_started_at > 0
    assert len(mgr.state.S.emotion_history) == 1
    assert mgr.state.S.emotion_history[0]["emotion"] == "开心"

    # 再次更新, 历史累积
    mgr.update_short_term(emotion="好奇", user_emotion="好奇")
    assert mgr.state.S.current_emotion == "好奇"
    assert len(mgr.state.S.emotion_history) == 2
    # G3: _save 为 debounce，需 flush 立即写盘
    mgr.flush()
    # 持久化
    reloaded = MentalState.load(tmp_path / "mental_state.json")
    assert reloaded.S.current_emotion == "好奇"


# ── M 层: 添加主题/压力事件/里程碑 ────────────────────


def test_m_layer_add_theme(tmp_path):
    """M 层添加主题"""
    mgr = MentalStateManager(data_dir=tmp_path)
    assert mgr.state.M.recent_themes == []

    mgr.add_theme("工作项目")
    mgr.add_theme("编程调试")
    mgr.add_theme("工作项目")  # 重复不添加
    assert mgr.state.M.recent_themes == ["工作项目", "编程调试"]


def test_m_layer_add_stress_event(tmp_path):
    """M 层添加压力事件: 自动补 ts 字段"""
    mgr = MentalStateManager(data_dir=tmp_path)
    assert mgr.state.M.stress_events == []

    mgr.add_stress_event({"event": "deadline", "level": "high"})
    assert len(mgr.state.M.stress_events) == 1
    evt = mgr.state.M.stress_events[0]
    assert evt["event"] == "deadline"
    assert "ts" in evt
    assert evt["ts"] > 0

    # 已带 ts 的事件不被覆盖
    custom_ts = 1000.0
    mgr.add_stress_event({"event": "old", "ts": custom_ts})
    assert mgr.state.M.stress_events[-1]["ts"] == custom_ts


def test_m_layer_add_milestone(tmp_path):
    """M 层添加关系里程碑"""
    mgr = MentalStateManager(data_dir=tmp_path)
    mgr.add_milestone({"event": "成为朋友", "level": "important"})
    assert len(mgr.state.M.relationship_milestones) == 1
    assert mgr.state.M.relationship_milestones[0]["event"] == "成为朋友"
    assert "ts" in mgr.state.M.relationship_milestones[0]


# ── Dream 整合: 清理 7 天前数据 ───────────────────────


def test_dream_consolidation(tmp_path):
    """Dream 时清理 7 天前的 M 层数据"""
    mgr = MentalStateManager(data_dir=tmp_path)
    now = time.time()

    # 添加一条 8 天前的压力事件 (应被清理)
    mgr.add_stress_event({"event": "old_stress", "ts": now - 8 * 86400})
    # 添加一条 3 天前的压力事件 (应保留)
    mgr.add_stress_event({"event": "recent_stress", "ts": now - 3 * 86400})
    # 添加一条 8 天前的里程碑 (应被清理)
    mgr.add_milestone({"event": "old_milestone", "ts": now - 8 * 86400})
    # 添加一条 1 天前的里程碑 (应保留)
    mgr.add_milestone({"event": "recent_milestone", "ts": now - 86400})

    assert len(mgr.state.M.stress_events) == 2
    assert len(mgr.state.M.relationship_milestones) == 2

    mgr.consolidate_dream()

    # 7 天前的数据被清理
    assert len(mgr.state.M.stress_events) == 1
    assert mgr.state.M.stress_events[0]["event"] == "recent_stress"
    assert len(mgr.state.M.relationship_milestones) == 1
    assert mgr.state.M.relationship_milestones[0]["event"] == "recent_milestone"
    # last_dream_at 已更新
    assert mgr.state.M.last_dream_at > 0


def test_dream_consolidation_via_dream_consolidator(tmp_path, monkeypatch):
    """DreamConsolidator.consolidate() 联动触发 mental state 清理 7 天前数据"""
    import asyncio

    from core.dream_consolidation import DreamConsolidator, Memory
    from core.mental_state import get_mental_state_manager, reset_mental_state_manager

    monkeypatch.setenv("MENTAL_STATE_ENABLED", "1")
    reset_mental_state_manager()
    mgr = get_mental_state_manager(data_dir=tmp_path)
    now = time.time()
    mgr.add_stress_event({"event": "old_stress", "ts": now - 8 * 86400})
    mgr.add_stress_event({"event": "new_stress", "ts": now})
    assert len(mgr.state.M.stress_events) == 2

    dream = DreamConsolidator()
    dream.add_memory(Memory(id="m1", content="test", importance=0.7))
    asyncio.run(dream.consolidate())

    # 7 天前数据被 Dream 联动清理
    assert len(mgr.state.M.stress_events) == 1
    assert mgr.state.M.stress_events[0]["event"] == "new_stress"
    reset_mental_state_manager()


def test_dream_consolidator_no_side_effect_when_uninitialized():
    """DreamConsolidator.consolidate() 在 mental state 未初始化时不创建副作用"""
    import asyncio

    from core.dream_consolidation import DreamConsolidator, Memory
    from core.mental_state import get_mental_state_manager_if_exists, reset_mental_state_manager

    reset_mental_state_manager()
    assert get_mental_state_manager_if_exists() is None
    dream = DreamConsolidator()
    dream.add_memory(Memory(id="m1", content="test", importance=0.7))
    # 不应抛异常, 也不应创建单例
    asyncio.run(dream.consolidate())
    assert get_mental_state_manager_if_exists() is None
    reset_mental_state_manager()


# ── Prompt 段落格式 ───────────────────────────────────


def test_prompt_segment_format(tmp_path):
    """prompt 段落格式正确: 包含 L/M/S 三层信息"""
    mgr = MentalStateManager(data_dir=tmp_path)
    mgr.state.L.core_traits = ["温柔", "聪慧", "耐心"]
    mgr.state.M.recent_themes = ["工作项目", "编程"]
    mgr.state.S.user_last_emotion = "焦虑"

    segment = mgr.get_prompt_segment()
    assert "[当前心理状态]" in segment
    assert "温柔、聪慧、耐心" in segment
    assert "工作项目" in segment
    assert "焦虑" in segment
    assert "安抚" in segment  # 焦虑 → 安抚语气


def test_prompt_segment_empty(tmp_path):
    """空状态时 prompt 段落仅含标题"""
    mgr = MentalStateManager(data_dir=tmp_path)
    segment = mgr.get_prompt_segment()
    assert "[当前心理状态]" in segment


# ── 环境变量开关 ───────────────────────────────────────


def test_env_var_disable(tmp_path, monkeypatch):
    """MENTAL_STATE_ENABLED=0 时功能关闭, 不更新状态"""
    monkeypatch.setenv("MENTAL_STATE_ENABLED", "0")
    mgr = MentalStateManager(data_dir=tmp_path)
    assert mgr.enabled is False

    # update_short_term 不生效
    mgr.update_short_term(emotion="开心", user_emotion="喜悦")
    assert mgr.state.S.current_emotion == ""
    assert mgr.state.S.emotion_history == []

    # add_theme 不生效
    mgr.add_theme("test")
    assert mgr.state.M.recent_themes == []

    # prompt 段落为空
    assert mgr.get_prompt_segment() == ""


def test_env_var_enable_default(tmp_path, monkeypatch):
    """未设置环境变量时默认开启"""
    monkeypatch.delenv("MENTAL_STATE_ENABLED", raising=False)
    mgr = MentalStateManager(data_dir=tmp_path)
    assert mgr.enabled is True


# ── 单例 ───────────────────────────────────────────────


def test_singleton(tmp_path, monkeypatch):
    """get_mental_state_manager 返回全局单例"""
    monkeypatch.setenv("MENTAL_STATE_ENABLED", "1")
    reset_mental_state_manager()
    m1 = get_mental_state_manager(data_dir=tmp_path)
    m2 = get_mental_state_manager()
    assert m1 is m2
    reset_mental_state_manager()
