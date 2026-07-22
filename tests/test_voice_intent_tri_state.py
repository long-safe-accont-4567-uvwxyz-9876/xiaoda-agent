"""测试三态语音意图检测：none / on / off

回归: 生产样本 id=1993 用户说"不要发语音了"但 TTS 仍生成（_detect_voice_intent 不处理否定）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_detect_voice_intent_off_negation():
    """否定+语音词 → off"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("不要发语音了") == "off"
    assert p._detect_voice_intent("不用语音了") == "off"
    assert p._detect_voice_intent("别发语音") == "off"
    assert p._detect_voice_intent("关掉语音") == "off"
    assert p._detect_voice_intent("关闭语音模式") == "off"


def test_detect_voice_intent_on_positive():
    """语音词无否定 → on"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("发语音") == "on"
    assert p._detect_voice_intent("听你说") == "on"
    assert p._detect_voice_intent("用声音回答") == "on"
    assert p._detect_voice_intent("念给我听") == "on"
    assert p._detect_voice_intent("语音回复") == "on"


def test_detect_voice_intent_none_no_keyword():
    """无语音词 → none"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("你好") == "none"
    assert p._detect_voice_intent("画一张图") == "none"
    assert p._detect_voice_intent("（静静地看着她）") == "none"


def test_detect_voice_intent_off_not_triggered_by_false_positive():
    """含"不"但非关语音 → 不误判（如"不太好"无语音词→none）"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("不太好吧") == "none"
    assert p._detect_voice_intent("不用了谢谢") == "none"


def test_detect_voice_intent_production_sample_1993():
    """生产样本 id=1993: '（还有不要发语音了）' → off"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    # 用户消息含 OOC 指令"不要发语音了"
    text = "（静静地看着她，微笑着不说话，只是手轻轻地搭在她的腰上）（还有不要发语音了）"
    assert p._detect_voice_intent(text) == "off"
