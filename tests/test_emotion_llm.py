"""LLM 深度情绪分析层测试

测试覆盖:
- _build_prompt: 正确构建提示文本
- _parse_llm_response: 解析 JSON / markdown 包裹 / 额外文本 / 字段范围钳制 / 空响应
- _clamp: 数值范围限制
- detect_emotion_llm: 空文本 / 无 router / 正常 / 超时 / 异常
"""
import asyncio

import pytest

from emotion.emotion_llm import (
    _build_prompt,
    _clamp,
    _parse_llm_response,
    detect_emotion_llm,
)


# ── Mock Router ──────────────────────────────────────────────


class MockRouter:
    """模拟模型路由器，返回类似 OpenAI 响应的对象"""

    def __init__(self, response_text, delay=0, raise_error=None):
        self._response = response_text
        self._delay = delay
        self._raise_error = raise_error
        self.last_route_name = None
        self.last_messages = None
        self.last_temperature = None

    async def route(self, route_name, messages, temperature):
        self.last_route_name = route_name
        self.last_messages = messages
        self.last_temperature = temperature
        if self._raise_error is not None:
            raise self._raise_error
        if self._delay:
            await asyncio.sleep(self._delay)

        class MockMessage:
            content = self._response

        class MockChoice:
            message = MockMessage()

        class MockResult:
            choices = [MockChoice()]

        return MockResult()


# ── _build_prompt ────────────────────────────────────────────


class TestBuildPrompt:
    """_build_prompt 正确构建提示文本"""

    def test_prompt_contains_user_text(self):
        prompt = _build_prompt("我今天好累啊", "")
        assert "我今天好累啊" in prompt

    def test_prompt_contains_context(self):
        prompt = _build_prompt("好累", "用户连续加班三天")
        assert "用户连续加班三天" in prompt

    def test_prompt_without_context_omits_context_line(self):
        prompt = _build_prompt("好开心", "")
        assert "上下文" not in prompt

    def test_prompt_wraps_text_with_brackets(self):
        prompt = _build_prompt("你好", "")
        assert "「你好」" in prompt


# ── _parse_llm_response ──────────────────────────────────────


class TestParseLlmResponse:
    """_parse_llm_response 解析 LLM JSON 响应"""

    def test_parse_normal_json(self):
        raw = '{"primary": "悲伤", "P": -0.6, "A": 0.3, "D": 0.2, "needs": ["休息", "被理解"], "style": "温柔陪伴"}'
        result = _parse_llm_response(raw)
        assert result["primary"] == "悲伤"
        assert result["P"] == pytest.approx(-0.6)
        assert result["A"] == pytest.approx(0.3)
        assert result["D"] == pytest.approx(0.2)
        assert result["needs"] == ["休息", "被理解"]
        assert result["style"] == "温柔陪伴"

    def test_parse_markdown_wrapped_json(self):
        raw = '```json\n{"primary": "喜悦", "P": 0.8, "A": 0.6, "D": 0.7, "needs": ["分享"], "style": "轻快回应"}\n```'
        result = _parse_llm_response(raw)
        assert result["primary"] == "喜悦"
        assert result["P"] == pytest.approx(0.8)
        assert result["style"] == "轻快回应"

    def test_parse_markdown_without_lang_tag(self):
        raw = '```\n{"primary": "平静", "P": 0.0, "A": 0.0, "D": 0.5, "needs": [], "style": ""}\n```'
        result = _parse_llm_response(raw)
        assert result["primary"] == "平静"

    def test_parse_extracts_json_from_extra_text(self):
        raw = '好的，我来分析用户的情绪：\n{"primary": "焦虑", "P": -0.4, "A": 0.7, "D": 0.3, "needs": ["安全感"], "style": "认真倾听"}\n以上是分析结果。'
        result = _parse_llm_response(raw)
        assert result["primary"] == "焦虑"
        assert result["needs"] == ["安全感"]
        assert result["style"] == "认真倾听"

    def test_clamp_P_to_range(self):
        raw = '{"primary": "愤怒", "P": 2.5, "A": 0.5, "D": 0.5, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["P"] == pytest.approx(1.0)

    def test_clamp_P_negative(self):
        raw = '{"primary": "悲伤", "P": -3.0, "A": 0.2, "D": 0.1, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["P"] == pytest.approx(-1.0)

    def test_clamp_A_to_range(self):
        raw = '{"primary": "兴奋", "P": 0.5, "A": 1.8, "D": 0.5, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["A"] == pytest.approx(1.0)

    def test_clamp_A_negative_to_zero(self):
        raw = '{"primary": "平静", "P": 0.0, "A": -0.5, "D": 0.5, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["A"] == pytest.approx(0.0)

    def test_clamp_D_to_range(self):
        raw = '{"primary": "好奇", "P": 0.2, "A": 0.4, "D": 2.0, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["D"] == pytest.approx(1.0)

    def test_clamp_D_negative_to_zero(self):
        raw = '{"primary": "恐惧", "P": -0.5, "A": 0.8, "D": -1.0, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["D"] == pytest.approx(0.0)

    def test_empty_response_returns_empty_dict(self):
        assert _parse_llm_response("") == {}

    def test_whitespace_response_returns_empty_dict(self):
        assert _parse_llm_response("   \n  ") == {}

    def test_invalid_json_returns_empty_dict(self):
        assert _parse_llm_response("这不是 JSON") == {}

    def test_no_braces_returns_empty_dict(self):
        assert _parse_llm_response("just some text without braces") == {}

    def test_default_primary_when_missing(self):
        raw = '{"P": 0.1, "A": 0.2, "D": 0.3, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["primary"] == "平静"

    def test_default_D_when_missing(self):
        raw = '{"primary": "好奇", "P": 0.1, "A": 0.2, "needs": [], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["D"] == pytest.approx(0.5)

    def test_result_has_all_keys(self):
        raw = '{"primary": "喜悦", "P": 0.5, "A": 0.5, "D": 0.5, "needs": ["x"], "style": "y"}'
        result = _parse_llm_response(raw)
        assert set(result.keys()) == {"primary", "P", "A", "D", "needs", "style"}

    def test_needs_filters_empty_entries(self):
        raw = '{"primary": "悲伤", "P": -0.3, "A": 0.2, "D": 0.4, "needs": ["休息", "", "被理解"], "style": ""}'
        result = _parse_llm_response(raw)
        assert result["needs"] == ["休息", "被理解"]


# ── _clamp ───────────────────────────────────────────────────


class TestClamp:
    """_clamp 数值范围限制"""

    def test_value_within_range_unchanged(self):
        assert _clamp(5, 0, 10) == pytest.approx(5)

    def test_value_below_min_clamped_to_min(self):
        assert _clamp(-5, 0, 10) == pytest.approx(0)

    def test_value_above_max_clamped_to_max(self):
        assert _clamp(15, 0, 10) == pytest.approx(10)

    def test_value_equals_min(self):
        assert _clamp(0, 0, 10) == pytest.approx(0)

    def test_value_equals_max(self):
        assert _clamp(10, 0, 10) == pytest.approx(10)

    def test_negative_range_P(self):
        assert _clamp(0.5, -1.0, 1.0) == pytest.approx(0.5)
        assert _clamp(-0.5, -1.0, 1.0) == pytest.approx(-0.5)

    def test_float_values(self):
        assert _clamp(0.3, 0.0, 1.0) == pytest.approx(0.3)


# ── detect_emotion_llm ───────────────────────────────────────


class TestDetectEmotionLlm:
    """detect_emotion_llm 主函数"""

    async def test_empty_text_returns_empty_dict(self):
        result = await detect_emotion_llm("", router=MockRouter("{}"))
        assert result == {}

    async def test_whitespace_text_returns_empty_dict(self):
        result = await detect_emotion_llm("   ", router=MockRouter("{}"))
        assert result == {}

    async def test_no_router_returns_empty_dict(self):
        # router=None 且全局 router 不可用时返回空字典
        result = await detect_emotion_llm("我今天好累啊", router=None)
        assert result == {}

    async def test_normal_response_with_mock_router(self):
        response = '{"primary": "悲伤", "P": -0.6, "A": 0.3, "D": 0.2, "needs": ["休息", "被理解"], "style": "温柔陪伴"}'
        router = MockRouter(response)
        result = await detect_emotion_llm("我今天好累啊", context="连续加班", router=router)
        assert result["primary"] == "悲伤"
        assert result["P"] == pytest.approx(-0.6)
        assert result["needs"] == ["休息", "被理解"]
        assert result["style"] == "温柔陪伴"

    async def test_router_called_with_chat_flash_and_temperature(self):
        response = '{"primary": "平静", "P": 0.0, "A": 0.0, "D": 0.5, "needs": [], "style": ""}'
        router = MockRouter(response)
        await detect_emotion_llm("你好", router=router)
        assert router.last_route_name == "chat_flash"
        assert router.last_temperature == pytest.approx(0.3)

    async def test_router_receives_messages_with_user_content(self):
        response = '{"primary": "平静", "P": 0.0, "A": 0.0, "D": 0.5, "needs": [], "style": ""}'
        router = MockRouter(response)
        await detect_emotion_llm("我今天好累啊", context="加班", router=router)
        assert router.last_messages is not None
        # messages 应包含 system 和 user 两条
        roles = [m["role"] for m in router.last_messages]
        assert "system" in roles
        assert "user" in roles
        user_msg = next(m for m in router.last_messages if m["role"] == "user")
        assert "我今天好累啊" in user_msg["content"]
        assert "加班" in user_msg["content"]

    async def test_timeout_returns_empty_dict(self):
        # 延迟 0.6s 超过 500ms 超时
        router = MockRouter('{"primary": "悲伤", "P": -0.6, "A": 0.3, "D": 0.2, "needs": [], "style": ""}', delay=0.6)
        result = await detect_emotion_llm("我今天好累啊", router=router)
        assert result == {}

    async def test_exception_returns_empty_dict(self):
        router = MockRouter("", raise_error=RuntimeError("LLM 调用失败"))
        result = await detect_emotion_llm("我今天好累啊", router=router)
        assert result == {}

    async def test_string_response_handled(self):
        # router 直接返回字符串也能处理
        class StringRouter:
            async def route(self, route_name, messages, temperature):
                return '{"primary": "喜悦", "P": 0.7, "A": 0.5, "D": 0.6, "needs": ["分享"], "style": "轻快回应"}'

        result = await detect_emotion_llm("好开心", router=StringRouter())
        assert result["primary"] == "喜悦"
        assert result["P"] == pytest.approx(0.7)

    async def test_nested_json_parsed_correctly(self):
        """LLM 返回嵌套 JSON 时能正确解析外层对象"""
        class NestedRouter:
            async def route(self, route_name, messages, temperature):
                return '{"primary": "悲伤", "P": -0.6, "A": 0.3, "D": 0.2, "needs": ["休息"], "style": "温柔陪伴", "meta": {"confidence": 0.9}}'

        result = await detect_emotion_llm("我好累", router=NestedRouter())
        assert result["primary"] == "悲伤"
        assert result["P"] == pytest.approx(-0.6)
        assert result["style"] == "温柔陪伴"
