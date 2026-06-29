"""XP 等级 → 亲密度 prompt 段落 + 等级模板测试。

覆盖:
- test_xp_segment_lv1: LV1 段落正确（礼貌克制、不主动提及私人话题）
- test_xp_segment_lv3: LV3 段落正确（可使用昵称、深度情感陪伴）
- test_xp_segment_no_user_id: 无 user_id 返回空字符串（向后兼容）
- test_levelup_triggers_ws_event: 升级触发 WS xp_levelup 事件（含 xp 字段）
- test_intimacy_config_load: 亲密度配置从 persona_levels.yaml 加载正确
"""
import asyncio
import sys
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
    """独立的临时 XP 系统（不污染单例）"""
    import core.xp_system as mod
    mod._reset_persona_cache()
    return mod.XPSystem(data_dir=tmp_path)


# ============================================================
# test_xp_segment_lv1
# ============================================================

def test_xp_segment_lv1(tmp_xp):
    """LV1 段落正确：礼貌克制、不主动提及私人话题"""
    from prompt_builder import _build_xp_segment
    # u_lv1: 0 XP → LV1 陌生人
    tmp_xp.get_state("u_lv1")

    with patch("core.xp_system.get_xp_system", return_value=tmp_xp):
        seg = _build_xp_segment("u_lv1")

    assert seg != ""
    assert "LV1" in seg
    assert "陌生人" in seg
    assert "polite" in seg
    # LV1 应保持礼貌克制、不主动提及私人话题
    assert "礼貌克制" in seg
    assert "不主动提及私人话题" in seg


# ============================================================
# test_xp_segment_lv3
# ============================================================

def test_xp_segment_lv3(tmp_xp):
    """LV3 段落正确：可使用昵称、深度情感陪伴"""
    from prompt_builder import _build_xp_segment
    from core.xp_system import XPLevel
    # 加 500 XP → LV3 朋友
    tmp_xp.add_xp("u_lv3", 500, "test", "")
    assert tmp_xp.get_state("u_lv3").level == XPLevel.LV3_FRIEND

    with patch("core.xp_system.get_xp_system", return_value=tmp_xp):
        seg = _build_xp_segment("u_lv3")

    assert seg != ""
    assert "LV3" in seg
    assert "朋友" in seg
    assert "intimate" in seg
    # LV3 应可使用昵称 + 深度情感陪伴
    assert "昵称" in seg
    assert "深度情感陪伴" in seg
    # XP 数值应出现在段落中
    assert "500" in seg


# ============================================================
# test_xp_segment_no_user_id
# ============================================================

def test_xp_segment_no_user_id():
    """无 user_id（None / 空串）返回空字符串，向后兼容"""
    from prompt_builder import _build_xp_segment
    assert _build_xp_segment(None) == ""
    assert _build_xp_segment("") == ""


# ============================================================
# test_levelup_triggers_ws_event
# ============================================================

def test_levelup_triggers_ws_event(tmp_xp):
    """升级触发 WS xp_levelup 事件，且事件包含 xp 字段供前端动画使用"""
    broadcast_mock = AsyncMock()
    fake_manager = type("FakeManager", (), {"broadcast": broadcast_mock})()

    with patch("web.ws_hub.manager", fake_manager):
        async def _run():
            # 95 + 5 = 100 → LV1→LV2
            tmp_xp.add_xp("u1", 95, "test", "")
            tmp_xp.add_xp("u1", 5, "test", "")
            # 让 create_task 调度完成
            await asyncio.sleep(0.01)

        asyncio.run(_run())

    assert broadcast_mock.await_count >= 1
    last_event = broadcast_mock.await_args.args[0]
    assert last_event["type"] == "xp_levelup"
    assert last_event["user_id"] == "u1"
    assert last_event["old_level"] == 1
    assert last_event["new_level"] == 2
    assert last_event["new_label"] == "LV2 熟人"
    # 新增 xp 字段，供 WebUI 升级动画展示当前进度
    assert last_event["xp"] == 100


# ============================================================
# test_intimacy_config_load
# ============================================================

def test_intimacy_config_load(tmp_xp):
    """亲密度配置从 config/persona_levels.yaml 正确加载（5 级关键字段）"""
    from core.xp_system import XPLevel

    # LV5 灵魂伴侣
    cfg5 = tmp_xp.get_intimacy_config(XPLevel.LV5_SOULMATE)
    assert cfg5["label"] == "灵魂伴侣"
    assert cfg5["tone"] == "soulmate"
    assert cfg5["address_term"] == "{nickname}"
    assert cfg5["initiative"] == 1.0
    assert cfg5["emotion_richness"] == 1.0
    assert cfg5["can_share_secrets"] is True

    # LV1 陌生人
    cfg1 = tmp_xp.get_intimacy_config(XPLevel.LV1_STRANGER)
    assert cfg1["label"] == "陌生人"
    assert cfg1["tone"] == "polite"
    assert cfg1["address_term"] == "你"
    assert cfg1["can_use_nickname"] is False
    assert cfg1["can_mention_past"] is False

    # LV3 朋友：可使用昵称但不可分享秘密
    cfg3 = tmp_xp.get_intimacy_config(XPLevel.LV3_FRIEND)
    assert cfg3["can_use_nickname"] is True
    assert cfg3["can_share_secrets"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
