"""测试情绪/sticker 标签泄漏修复

验证：
1. [emotion:xxx] 标准格式被正确移除
2. [playful/stickers:xxx] LLM 幻觉格式被正确移除
3. [happy] [playful] 等纯情绪词标签被移除
4. 正常文本不受影响
"""
import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


class TestStickerTagLeak:
    """测试情绪/sticker 标签泄漏修复"""

    def test_standard_emotion_tag_removed(self):
        """[emotion:xxx] 标准格式被移除"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        result = mgr.strip_emotion_tag("你好呀～ [emotion:happy]")
        assert "[emotion:" not in result
        assert "你好呀～" in result

    def test_playful_stickers_tag_removed(self):
        """[playful/stickers:xxx] LLM 幻觉格式被移除"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        text = "嘻嘻 [playful/stickers:b7f198e0-dcf5-4b4c-b6a2-e63bd1cc6a9c /emotions/f29d7fbb-a48b-43eb-b2aa-db1ef6faee1e.png]"
        result = mgr.strip_emotion_tag(text)
        assert "[playful/" not in result
        assert "stickers:" not in result
        assert "嘻嘻" in result

    def test_pure_emotion_word_tag_removed(self):
        """[happy] [playful] 等纯情绪词标签被移除"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        result = mgr.strip_emotion_tag("好开心呀 [happy]")
        assert "[happy]" not in result
        assert "好开心呀" in result

    def test_playful_tag_removed(self):
        """[playful] 标签被移除"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        result = mgr.strip_emotion_tag("嘻嘻～ [playful]")
        assert "[playful]" not in result

    def test_normal_text_unchanged(self):
        """正常文本不受影响"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        text = "今天天气真好呀～我们去玩吧！"
        result = mgr.strip_emotion_tag(text)
        assert result == text

    def test_truncated_sticker_tag_removed(self):
        """截断的 sticker 标签也被移除"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        # 模拟被 max_tokens 截断的 sticker 标签
        text = "哇！好开心！[playful/sti"
        result = mgr.strip_emotion_tag(text)
        assert "[playful" not in result

    def test_multiple_tags_removed(self):
        """多个标签同时存在时全部移除"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        text = "你好 [emotion:happy] 嘻嘻 [playful]"
        result = mgr.strip_emotion_tag(text)
        assert "[emotion:" not in result
        assert "[playful]" not in result
        assert "你好" in result
        assert "嘻嘻" in result

    def test_full_leaked_reply_from_log(self):
        """模拟日志中实际泄漏的回复"""
        from emotion.sticker_manager import StickerManager
        mgr = StickerManager.__new__(StickerManager)
        # 模拟 7/12 日志中的实际回复
        text = (
            '哇！听起来朋友最近发现新玩具啦，好开心！[happy]  \n\n'
            '"堵桥"这个词一出来……感觉是那种在必经之路上埋伏、'
            '等别人踩雷的好玩机制呢？像不像游戏版"伏地魔蹲草丛"嘛～'
            '嘻嘻 [playful/stickers:b7f198e0-dcf5-4b4c-b6a2-e63bd1cc6a9c '
            '/emotions/f29d7fbb-a48b-43eb-b2aa-db1ef6faee1e.png]'
        )
        result = mgr.strip_emotion_tag(text)
        assert "[happy]" not in result
        assert "[playful/" not in result
        assert "stickers:" not in result
        assert "/emotions/" not in result
        assert "哇！听起来朋友" in result
