"""XP 等级成长系统测试

覆盖:
- test_initial_state: 新用户初始化为 LV1 + 0 XP
- test_add_chat_xp_short: 短消息 +1 XP
- test_add_chat_xp_deep: 长消息 +5 XP
- test_add_support_xp: 情感支持 +10 XP
- test_add_task_xp: 共同任务 +20 XP
- test_daily_login_once_per_day: 每日登录只触发一次
- test_levelup_trigger: 升级触发并记录里程碑
- test_persistence: 保存/加载往返
- test_compute_level_thresholds: 各等级阈值计算
- test_disabled_via_env: 环境变量关闭后 XP 不再累积
- test_levelup_pushes_ws_event: 升级时通过 ws_hub 推送事件
- test_persona_config_loads_yaml: 亲密度配置从 YAML 加载
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# 确保项目根在 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _ensure_xp_enabled(monkeypatch):
    """每个测试默认启用 XP 系统"""
    monkeypatch.setenv("XP_SYSTEM_ENABLED", "true")


@pytest.fixture
def tmp_xp(tmp_path, monkeypatch):
    """每个测试用独立的临时数据目录, 避免污染单例"""
    import core.xp_system as mod
    # 重置 persona 缓存, 让测试隔离
    mod._reset_persona_cache()
    return mod.XPSystem(data_dir=tmp_path)


# ============================================================
# test_initial_state
# ============================================================

def test_initial_state(tmp_xp):
    """新用户初始化为 LV1 + 0 XP"""
    state = tmp_xp.get_state("u_new")
    assert state.user_id == "u_new"
    assert state.xp == 0
    assert state.level == tmp_xp._compute_level(0)
    # 默认应为 LV1 陌生人
    from core.xp_system import XPLevel
    assert state.level == XPLevel.LV1_STRANGER
    assert state.history == []
    assert state.milestones == []
    assert state.first_seen_at > 0


# ============================================================
# test_add_chat_xp_short
# ============================================================

def test_add_chat_xp_short(tmp_xp):
    """短消息 +1 XP"""
    state, leveled_up = tmp_xp.add_chat_xp("u1", message_length=10)
    assert state.xp == 1
    assert not leveled_up
    assert len(state.history) == 1
    assert state.history[0].source == "chat"
    assert state.history[0].amount == 1


# ============================================================
# test_add_chat_xp_deep
# ============================================================

def test_add_chat_xp_deep(tmp_xp):
    """长消息 (>=50 字) +5 XP"""
    state, leveled_up = tmp_xp.add_chat_xp("u1", message_length=50)
    assert state.xp == 5
    assert not leveled_up
    assert state.history[-1].source == "deep_chat"
    assert state.history[-1].amount == 5

    # 边界: 49 字仍是普通 +1, 50 字是深度 +5
    state2, _ = tmp_xp.add_chat_xp("u2", message_length=49)
    assert state2.xp == 1
    state3, _ = tmp_xp.add_chat_xp("u3", message_length=51)
    assert state3.xp == 5


# ============================================================
# test_add_support_xp
# ============================================================

def test_add_support_xp(tmp_xp):
    """情感支持 +10 XP"""
    state, leveled_up = tmp_xp.add_support_xp("u1")
    assert state.xp == 10
    assert not leveled_up
    assert state.history[-1].source == "support"
    assert state.history[-1].amount == 10


# ============================================================
# test_add_task_xp
# ============================================================

def test_add_task_xp(tmp_xp):
    """共同完成任务 +20 XP"""
    state, leveled_up = tmp_xp.add_task_xp("u1")
    assert state.xp == 20
    assert not leveled_up
    assert state.history[-1].source == "task_collab"
    assert state.history[-1].amount == 20


# ============================================================
# test_daily_login_once_per_day
# ============================================================

def test_daily_login_once_per_day(tmp_xp):
    """每日首次登录 +5 XP, 同一天只触发一次"""
    # 当天首次登录
    state1, leveled1 = tmp_xp.add_daily_login_xp("u1")
    assert state1.xp == 5
    assert not leveled1
    assert state1.last_daily_login_date != ""

    # 同一天再次调用: 不应再加 XP
    state2, leveled2 = tmp_xp.add_daily_login_xp("u1")
    assert state2.xp == 5  # 没有增加
    assert not leveled2
    assert state2.last_daily_login_date == state1.last_daily_login_date


def test_daily_login_next_day(tmp_xp, monkeypatch):
    """跨天后可再次领取每日登录 XP"""
    # 第一天
    tmp_xp.add_daily_login_xp("u1")
    assert tmp_xp.get_state("u1").xp == 5

    # 模拟第二天: 修改 last_daily_login_date 为昨天
    today = time.strftime("%Y-%m-%d", time.localtime())
    yesterday_ts = time.time() - 86400
    yesterday = time.strftime("%Y-%m-%d", time.localtime(yesterday_ts))
    tmp_xp.get_state("u1").last_daily_login_date = yesterday
    tmp_xp._save()

    # 重新实例化 (模拟重启), 验证持久化也保留
    import core.xp_system as mod
    sys2 = mod.XPSystem(data_dir=tmp_xp._state_path.parent)
    state, leveled = sys2.add_daily_login_xp("u1")
    assert state.xp == 10  # 5 (第一天) + 5 (第二天)
    assert state.last_daily_login_date == today
    assert not leveled


# ============================================================
# test_levelup_trigger
# ============================================================

def test_levelup_trigger(tmp_xp):
    """升级触发并记录里程碑"""
    # u1 从 LV1 (0-100) → LV2 (100+)
    # 先加 95 XP 仍停留在 LV1
    state, leveled = tmp_xp.add_xp("u1", 95, "test", "near threshold")
    from core.xp_system import XPLevel
    assert state.level == XPLevel.LV1_STRANGER
    assert not leveled
    assert state.milestones == []

    # 再加 5 XP 越过 100 阈值, 升级到 LV2
    state, leveled = tmp_xp.add_xp("u1", 5, "test", "cross threshold")
    assert state.xp == 100
    assert state.level == XPLevel.LV2_ACQUAINTANCE
    assert leveled
    assert len(state.milestones) == 1
    milestone = state.milestones[0]
    assert milestone["from_level"] == 1
    assert milestone["to_level"] == 2
    assert milestone["xp_at_milestone"] == 100


def test_levelup_multiple(tmp_xp):
    """一次跨多级升级 (e.g. LV1→LV3) 也应被记录"""
    # 直接给 600 XP, 跨越 LV2(100) 和 LV3(500)
    state, leveled = tmp_xp.add_xp("u1", 600, "big_grant", "")
    from core.xp_system import XPLevel
    assert state.level == XPLevel.LV3_FRIEND
    assert leveled
    # 里程碑应记录 from=1, to=3 (跨级也只生成一条记录)
    assert len(state.milestones) == 1
    assert state.milestones[0]["from_level"] == 1
    assert state.milestones[0]["to_level"] == 3


# ============================================================
# test_persistence
# ============================================================

def test_persistence(tmp_path):
    """保存/加载往返: XP / level / history / milestones / daily_login_date 全保留"""
    import core.xp_system as mod
    sys1 = mod.XPSystem(data_dir=tmp_path)
    sys1.add_xp("u1", 100, "test", "to LV2")
    sys1.add_daily_login_xp("u1")
    state1 = sys1.get_state("u1")
    assert state1.xp == 105

    # 状态文件应已存在
    assert sys1._state_path.exists()

    # 新实例从同一文件加载
    mod._reset_persona_cache()
    sys2 = mod.XPSystem(data_dir=tmp_path)
    state2 = sys2.get_state("u1")
    assert state2.xp == state1.xp
    assert state2.level == state1.level
    assert state2.last_daily_login_date == state1.last_daily_login_date
    assert len(state2.history) == len(state1.history)
    assert len(state2.milestones) == len(state1.milestones)
    # 历史记录的内容应可还原
    assert state2.history[0].source == state1.history[0].source
    assert state2.history[0].amount == state1.history[0].amount


def test_persistence_valid_json(tmp_path):
    """持久化文件应是合法 JSON 且结构完整"""
    import json
    import core.xp_system as mod
    sys1 = mod.XPSystem(data_dir=tmp_path)
    sys1.add_xp("u1", 50, "chat", "")
    sys1._save()
    with open(sys1._state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "users" in data
    assert "updated_at" in data
    assert "u1" in data["users"]
    assert data["users"]["u1"]["xp"] == 50


def test_load_corrupted_file(tmp_path):
    """加载损坏文件时应回退到空状态, 不抛异常"""
    import core.xp_system as mod
    path = tmp_path / "xp_state.json"
    path.write_text("not a valid json {{{", encoding="utf-8")
    sys_inst = mod.XPSystem(data_dir=tmp_path)
    # 应不抛异常, 状态为空
    state = sys_inst.get_state("u_new")
    assert state.xp == 0


# ============================================================
# test_compute_level_thresholds
# ============================================================

def test_compute_level_thresholds(tmp_xp):
    """各等级阈值边界计算正确"""
    from core.xp_system import XPLevel

    assert tmp_xp._compute_level(0) == XPLevel.LV1_STRANGER
    assert tmp_xp._compute_level(99) == XPLevel.LV1_STRANGER
    assert tmp_xp._compute_level(100) == XPLevel.LV2_ACQUAINTANCE
    assert tmp_xp._compute_level(499) == XPLevel.LV2_ACQUAINTANCE
    assert tmp_xp._compute_level(500) == XPLevel.LV3_FRIEND
    assert tmp_xp._compute_level(1999) == XPLevel.LV3_FRIEND
    assert tmp_xp._compute_level(2000) == XPLevel.LV4_CLOSE_FRIEND
    assert tmp_xp._compute_level(4999) == XPLevel.LV4_CLOSE_FRIEND
    assert tmp_xp._compute_level(5000) == XPLevel.LV5_SOULMATE
    assert tmp_xp._compute_level(9999) == XPLevel.LV5_SOULMATE
    assert tmp_xp._compute_level(10000) == XPLevel.LV6_ETERNAL
    assert tmp_xp._compute_level(99999) == XPLevel.LV6_ETERNAL


# ============================================================
# test_disabled_via_env
# ============================================================

def test_disabled_via_env(tmp_path, monkeypatch):
    """环境变量 XP_SYSTEM_ENABLED=false 时, add_xp 不再累积"""
    monkeypatch.setenv("XP_SYSTEM_ENABLED", "false")
    import core.xp_system as mod
    mod._reset_persona_cache()
    sys_inst = mod.XPSystem(data_dir=tmp_path)
    state, leveled = sys_inst.add_xp("u1", 500, "test", "")
    assert state.xp == 0
    assert not leveled
    # chat_xp 同样不应累积
    state2, _ = sys_inst.add_chat_xp("u1", message_length=100)
    assert state2.xp == 0


# ============================================================
# test_levelup_pushes_ws_event
# ============================================================

def test_levelup_pushes_ws_event(tmp_xp):
    """升级时通过 web.ws_hub.manager.broadcast 推送 xp_levelup 事件"""
    broadcast_mock = AsyncMock()
    fake_manager = type("FakeManager", (), {"broadcast": broadcast_mock})()

    with patch("web.ws_hub.manager", fake_manager):
        # 在事件循环中调用 add_xp, 这样 _push_levelup_event 才能拿到 running loop
        async def _run():
            # 95 + 5 = 100 → LV1→LV2
            tmp_xp.add_xp("u1", 95, "test", "")
            tmp_xp.add_xp("u1", 5, "test", "")
            # 让 create_task 调度完成
            await asyncio.sleep(0.01)

        asyncio.run(_run())

    assert broadcast_mock.await_count >= 1
    # 最后一次 broadcast 应是升级事件
    last_event = broadcast_mock.await_args.args[0]
    assert last_event["type"] == "xp_levelup"
    assert last_event["user_id"] == "u1"
    assert last_event["old_level"] == 1
    assert last_event["new_level"] == 2
    assert last_event["new_label"] == "LV2 熟人"


def test_levelup_no_event_loop_is_safe(tmp_xp):
    """sync 上下文 (无事件循环) 中升级不应抛异常"""
    # 直接同步调用 add_xp, 此时无运行中的 event loop
    state, leveled = tmp_xp.add_xp("u1", 100, "test", "")
    assert leveled
    # 里程碑应仍被记录
    assert len(state.milestones) == 1


# ============================================================
# test_persona_config_loads_yaml
# ============================================================

def test_persona_config_loads_yaml(tmp_xp):
    """亲密度配置从 config/persona_levels.yaml 加载"""
    from core.xp_system import XPLevel
    # LV5 应有 soulmate tone 和 {nickname} 称呼
    cfg = tmp_xp.get_intimacy_config(XPLevel.LV5_SOULMATE)
    assert cfg["tone"] == "soulmate"
    assert cfg["address_term"] == "{nickname}"
    assert cfg["initiative"] == 1.0
    assert cfg["can_share_secrets"] is True

    # LV1 应不可使用昵称/提及过往
    cfg1 = tmp_xp.get_intimacy_config(XPLevel.LV1_STRANGER)
    assert cfg1["can_use_nickname"] is False
    assert cfg1["can_mention_past"] is False
    assert cfg1["tone"] == "polite"

    # LV3 应可使用昵称但不可分享秘密
    cfg3 = tmp_xp.get_intimacy_config(XPLevel.LV3_FRIEND)
    assert cfg3["can_use_nickname"] is True
    assert cfg3["can_share_secrets"] is False


def test_get_level_label(tmp_xp):
    """等级中文标签格式正确"""
    from core.xp_system import XPLevel
    assert tmp_xp.get_level_label(XPLevel.LV1_STRANGER) == "LV1 陌生人"
    assert tmp_xp.get_level_label(XPLevel.LV2_ACQUAINTANCE) == "LV2 熟人"
    assert tmp_xp.get_level_label(XPLevel.LV3_FRIEND) == "LV3 朋友"
    assert tmp_xp.get_level_label(XPLevel.LV4_CLOSE_FRIEND) == "LV4 挚友"
    assert tmp_xp.get_level_label(XPLevel.LV5_SOULMATE) == "LV5 灵魂伴侣"


# ============================================================
# test_get_singleton
# ============================================================

def test_get_xp_system_singleton():
    """get_xp_system 返回单例"""
    import core.xp_system as mod
    a = mod.get_xp_system()
    b = mod.get_xp_system()
    assert a is b


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
