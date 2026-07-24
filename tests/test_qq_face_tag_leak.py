"""QQ 表情标签 <faceType=.../> 泄漏修复测试。

根因：botpy 将用户发表情序列化为 <faceType=1,faceId="N",ext="base64"/> 塞进 message.content，
经对话历史被 LLM 模仿输出，清洗管线无对应规则 → 原始标签泄漏到 QQ 回复。

样本取自 2026-07-24 生产日志 (agent_2026-07-24.json)。
"""
from unittest.mock import MagicMock

from utils.llm_cleanup import strip_qq_face_tags
from agent_core.tool_executor import ToolExecutorMixin


# 生产日志样本：用户消息中的 QQ 表情标签（botpy 序列化，无自闭合斜杠）
USER_MSG_FACE_5 = '你凶我<faceType=1,faceId="5",ext="eyJ0ZXh0Ijoi5rWB5rOqIn0=">'
# 生产样本：带自闭合斜杠的变体
USER_MSG_FACE_339 = '一把推到，kisskisskiss<faceType=1,faceId="339",ext="eyJ0ZXh0Ijoi6IiU5bG"/>'
# 生产样本：LLM 回复中模仿输出的表情标签（faceId=24，ext 解码为 {"text":"害羞 的 你的体香很好闻"}）
LLM_REPLY_LEAK = (
    '呜呜啊啊啊啊啊——？！😱💦 （吓了一跳）呀——！！？爸爸……你在干嘛啦！！ >_< '
    '<faceType=1,faceId="24",ext="eyJ0ZXh0Ijoi57yW56S-IOeahOSuheWRm+SbhuS/g+aciOadgOmFniJ9="/>。'
)


class _Stub(ToolExecutorMixin):
    def __init__(self):
        self._sm = MagicMock()
        self._sm.strip_emotion_tag = MagicMock(side_effect=lambda t: t)

    def get_sticker_manager(self, name):
        return self._sm


def test_strip_user_message_face_tag():
    out = strip_qq_face_tags(USER_MSG_FACE_5)
    assert "<faceType" not in out
    assert "faceId" not in out
    assert "你凶我" in out


def test_strip_self_closing_variant():
    out = strip_qq_face_tags(USER_MSG_FACE_339)
    assert "<faceType" not in out
    assert "kisskisskiss" in out


def test_strip_llm_reply_leak_preserves_text():
    out = strip_qq_face_tags(LLM_REPLY_LEAK)
    assert "<faceType" not in out
    assert "faceId" not in out
    # 人格回复文本保留
    assert "呜呜啊啊啊啊啊" in out
    assert "爸爸" in out
    # 标签剥离后末尾句号仍在
    assert "。" in out


def test_clean_reply_full_strips_face_tag():
    m = _Stub()
    out = m._clean_reply_full(LLM_REPLY_LEAK, style="xiaoda", strip_emotion=False)
    assert "<faceType" not in out
    assert "faceId" not in out
    assert "呜呜啊啊啊啊啊" in out


def test_no_face_tag_is_noop():
    # 无标签时原样返回（fast-path，管线每次调用都走）
    plain = "普通的回复，没有任何标签～"
    assert strip_qq_face_tags(plain) == plain
