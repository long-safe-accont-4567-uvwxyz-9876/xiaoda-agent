"""测试表情包三级 fallback：文件名→情绪名→resolve_emotion→文本检测

回归: 生产日志反复 sticker.not_found name=crying/shy/surprised
LLM 输出 [sticker:情绪名] 而非 [sticker:文件名]，pick_by_name 失败后无 fallback。
"""
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_stub(sticker_dir: Path):
    """创建带 sticker_manager 的 ToolExecutorMixin stub"""
    from agent_core.tool_executor import ToolExecutorMixin
    from emotion.sticker_manager import StickerManager
    stub = ToolExecutorMixin.__new__(ToolExecutorMixin)
    stub.sticker_manager = StickerManager(sticker_dir)
    return stub


def test_sticker_by_emotion_name_shy(tmp_path):
    """[sticker:shy] → pick_by_name 失败 → pick('shy') 成功"""
    (tmp_path / "shy").mkdir()
    (tmp_path / "shy" / "shy_脸红.jpg").write_bytes(b"x")
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    stub = _make_stub(tmp_path)
    reply = "测试 [sticker:shy]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None
    assert "shy" in str(path)
    assert "[sticker:" not in clean


def test_sticker_crying_maps_to_sad(tmp_path):
    """[sticker:crying] → resolve_emotion('crying')→SAD → pick('sad') 成功"""
    (tmp_path / "sad").mkdir()
    (tmp_path / "sad" / "sad_含泪.jpg").write_bytes(b"x")
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    stub = _make_stub(tmp_path)
    reply = "呜呜 [sticker:crying]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None
    assert "sad" in str(path)


def test_sticker_surprised_maps_to_surprised(tmp_path):
    """[sticker:surprised] → pick('surprised') 成功"""
    (tmp_path / "surprised").mkdir()
    (tmp_path / "surprised" / "surprised_震惊.jpg").write_bytes(b"x")
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    stub = _make_stub(tmp_path)
    reply = "哇 [sticker:surprised]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None
    assert "surprised" in str(path)


def test_sticker_nonexistent_falls_back_to_neutral(tmp_path):
    """[sticker:不存在的] → 全部 fallback 失败 → neutral 兜底"""
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    stub = _make_stub(tmp_path)
    reply = "不存在的标签 [sticker:xyz_fake]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None  # neutral 兜底
    assert "[sticker:" not in clean


def test_sticker_exact_filename_still_works(tmp_path):
    """[sticker:happy_开心.jpg] → pick_by_name 精确匹配（原有行为不破坏）"""
    (tmp_path / "happy").mkdir()
    (tmp_path / "happy" / "happy_开心.jpg").write_bytes(b"x")
    stub = _make_stub(tmp_path)
    reply = "好开心 [sticker:happy_开心.jpg]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None
    assert path.name == "happy_开心.jpg"


def test_resolve_emotion_crying_alias():
    """crying → Emotion.SAD 别名"""
    from emotion.emotion_enum import resolve_emotion, Emotion
    assert resolve_emotion("crying") == Emotion.SAD
