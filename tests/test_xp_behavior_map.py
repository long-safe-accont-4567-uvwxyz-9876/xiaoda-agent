"""XP 等级 → 行为参数映射测试

覆盖:
- test_behavior_map_contains_all_levels: XP_BEHAVIOR_MAP 包含所有 6 个 XPLevel
- test_lv1_stranger_config: LV1 陌生人 proximity=far / initiate=False / special=[]
- test_lv3_friend_config: LV3 朋友 proximity=near / initiate=True / special 含 wave
- test_lv6_eternal_config: LV6 至死不渝 proximity=intimate / special 含 kiss
- test_get_behavior_config_lv3: get_behavior_config(LV3) 返回正确 dict
- test_get_behavior_config_fallback: 未知等级回退到 LV1 配置
- test_special_monotonically_increasing: 等级越高 special 列表越长(单调递增)
"""
import sys
from pathlib import Path

# 确保项目根在 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# test_behavior_map_contains_all_levels
# ============================================================

def test_behavior_map_contains_all_levels():
    """XP_BEHAVIOR_MAP 包含所有 6 个 XPLevel"""
    from core.xp_system import XP_BEHAVIOR_MAP, XPLevel
    for level in XPLevel:
        assert level in XP_BEHAVIOR_MAP, f"缺少等级: {level}"
    assert len(XP_BEHAVIOR_MAP) == 6


# ============================================================
# test_lv1_stranger_config
# ============================================================

def test_lv1_stranger_config():
    """LV1 陌生人: proximity=far / initiate=False / special=[]"""
    from core.xp_system import XP_BEHAVIOR_MAP, XPLevel
    cfg = XP_BEHAVIOR_MAP[XPLevel.LV1_STRANGER]
    assert cfg["proximity"] == "far"
    assert cfg["initiate"] is False
    assert cfg["special"] == []


# ============================================================
# test_lv3_friend_config
# ============================================================

def test_lv3_friend_config():
    """LV3 朋友: proximity=near / initiate=True / special 含 wave"""
    from core.xp_system import XP_BEHAVIOR_MAP, XPLevel
    cfg = XP_BEHAVIOR_MAP[XPLevel.LV3_FRIEND]
    assert cfg["proximity"] == "near"
    assert cfg["initiate"] is True
    assert "wave" in cfg["special"]


# ============================================================
# test_lv6_eternal_config
# ============================================================

def test_lv6_eternal_config():
    """LV6 至死不渝: proximity=intimate / special 含 kiss"""
    from core.xp_system import XP_BEHAVIOR_MAP, XPLevel
    cfg = XP_BEHAVIOR_MAP[XPLevel.LV6_ETERNAL]
    assert cfg["proximity"] == "intimate"
    assert "kiss" in cfg["special"]


# ============================================================
# test_get_behavior_config_lv3
# ============================================================

def test_get_behavior_config_lv3():
    """get_behavior_config(LV3_FRIEND) 返回正确的 dict"""
    from core.xp_system import XPLevel, get_behavior_config
    cfg = get_behavior_config(XPLevel.LV3_FRIEND)
    assert cfg["proximity"] == "near"
    assert cfg["initiate"] is True
    assert cfg["special"] == ["wave"]


# ============================================================
# test_get_behavior_config_fallback
# ============================================================

def test_get_behavior_config_fallback():
    """get_behavior_config 未知等级回退到 LV1_STRANGER 配置"""
    from core.xp_system import XP_BEHAVIOR_MAP, XPLevel, get_behavior_config
    # 传入不在映射中的值, 应回退到 LV1 配置
    unknown = object()
    cfg = get_behavior_config(unknown)  # type: ignore[arg-type]
    fallback = XP_BEHAVIOR_MAP[XPLevel.LV1_STRANGER]
    assert cfg == fallback
    assert cfg["proximity"] == "far"


# ============================================================
# test_special_monotonically_increasing
# ============================================================

def test_special_monotonically_increasing():
    """等级越高, special 列表越长(单调递增)"""
    from core.xp_system import XP_BEHAVIOR_MAP, XPLevel
    levels = [
        XPLevel.LV1_STRANGER,
        XPLevel.LV2_ACQUAINTANCE,
        XPLevel.LV3_FRIEND,
        XPLevel.LV4_CLOSE_FRIEND,
        XPLevel.LV5_SOULMATE,
        XPLevel.LV6_ETERNAL,
    ]
    lengths = [len(XP_BEHAVIOR_MAP[lv]["special"]) for lv in levels]
    for i in range(len(lengths) - 1):
        assert lengths[i] <= lengths[i + 1], (
            f"等级 {levels[i]} special={lengths[i]} "
            f"不应多于等级 {levels[i+1]} special={lengths[i+1]}"
        )
    # LV1 应为 0, LV6 应为最多
    assert lengths[0] == 0
    assert lengths[-1] == 4
