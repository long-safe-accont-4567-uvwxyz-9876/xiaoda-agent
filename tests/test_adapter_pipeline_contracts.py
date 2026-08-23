from __future__ import annotations

import asyncio
import time
from dataclasses import fields
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from channel_adapter_base import ChannelAdapterBase, CoreProcessRequest, TTLCache
from qq_bot_adapter import AIQQBot, QQPipelineRequest
from wechat_bot_adapter import WeChatBotAdapter, WxProcessRequest


class _Result:
    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.sticker_path = None


class _Message:
    def __init__(self) -> None:
        self.author = SimpleNamespace(user_openid="qq-openid")
        self.replies: list[str] = []

    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        self.replies.append(content)


def _qq_bot(core: Any | None = None) -> AIQQBot:
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = core
    bot.hitl_enabled = False
    bot.api = SimpleNamespace(post_c2c_message=AsyncMock(return_value=object()))
    return bot


def _wechat_bot(core: Any | None = None) -> WeChatBotAdapter:
    with patch.object(WeChatBotAdapter, "_load_cursor", return_value=""):
        return WeChatBotAdapter(
            db=object(),
            router=object(),
            api=None,
            user_openid="owner",
            core=core,
        )


def test_pipeline_request_fields_and_process_kwargs_are_channel_explicit() -> None:
    core_names = [field.name for field in fields(CoreProcessRequest)]
    qq_names = [field.name for field in fields(QQPipelineRequest)]

    assert core_names == ["text", "user_id", "source", "user_openid", "session_id"]
    assert "extra_kwargs" not in qq_names
    assert qq_names.count("image_data") == 1
    assert qq_names.count("is_master") == 1

    bot = _qq_bot()
    req = QQPipelineRequest(
        text="hello",
        user_id="qq-user",
        source="qq_c2c",
        user_openid="qq-openid",
        session_id="sid",
        image_data=[{"data": "abc"}],
        is_master=False,
        message=_Message(),
    )
    kwargs = bot._build_process_kwargs(req, req.session_id)

    assert set(kwargs) == {
        "user_id",
        "source",
        "user_openid",
        "status_callback",
        "session_id",
        "image_data",
        "is_master",
        "user_context_token_callback",
    }
    assert kwargs["image_data"] == [{"data": "abc"}]
    assert kwargs["is_master"] is False
    assert callable(kwargs["user_context_token_callback"])


def test_common_process_kwargs_omit_empty_session_and_channel_fields() -> None:
    adapter = ChannelAdapterBase()
    req = CoreProcessRequest(
        text="hello",
        user_id="user",
        source="test",
        user_openid="openid",
    )

    kwargs = adapter._build_process_kwargs(req, None)

    assert set(kwargs) == {"user_id", "source", "user_openid", "status_callback"}
    assert "session_id" not in kwargs


@pytest.mark.asyncio
async def test_wechat_unknown_session_error_propagates_without_fallback() -> None:
    core = SimpleNamespace(process=AsyncMock(return_value=_Result()))
    bot = _wechat_bot(core)
    bot._send_ack = AsyncMock()
    bot._resolve_session = AsyncMock(side_effect=LookupError("unknown session"))
    bot.send_message = AsyncMock(return_value=True)
    req = WxProcessRequest(
        text="hello",
        user_id="wechat-user",
        source="wechat_c2c",
        user_openid="wx-openid",
        context_token="ctx",
    )

    with pytest.raises(LookupError, match="unknown session"):
        await bot._process_with_core(req)

    core.process.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wechat_core_lookup_error_uses_channel_fallback() -> None:
    core = SimpleNamespace(process=AsyncMock(side_effect=LookupError("core failed")))
    bot = _wechat_bot(core)
    bot._send_ack = AsyncMock()
    bot._resolve_session = AsyncMock(return_value="sid")
    bot.send_message = AsyncMock(return_value=True)
    req = WxProcessRequest(
        text="hello",
        user_id="wechat-user",
        source="wechat_c2c",
        user_openid="wx-openid",
        context_token="ctx",
    )

    assert await bot._process_with_core(req) is None

    bot.send_message.assert_awaited_once_with(
        "出了点小问题，等会儿再聊好不好？",
        to_user_id="wx-openid",
        context_token="ctx",
    )


class _PipelineHarness(ChannelAdapterBase):
    CORE_ERROR_TYPES = (RuntimeError,)

    def __init__(self, process: AsyncMock, *, post_error: bool = False) -> None:
        self.core = SimpleNamespace(process=process)
        self.events: list[str] = []
        self.post_error = post_error

    def _get_core(self) -> Any:
        return self.core

    async def _send_ack(self, req: CoreProcessRequest) -> None:
        self.events.append("ack")

    async def _resolve_session(self, req: CoreProcessRequest) -> str:
        self.events.append("session")
        return "sid"

    def _bind_bus_user(self, req: CoreProcessRequest) -> str:
        self.events.append("bind")
        return "token"

    def _unbind_bus_user(self, token: Any) -> None:
        assert token == "token"
        self.events.append("unbind")

    async def _post_process_result(self, req: CoreProcessRequest, result: Any) -> Any:
        self.events.append("post")
        if self.post_error:
            raise RuntimeError("post failed")
        return result

    async def _on_core_timeout(self, req: CoreProcessRequest) -> None:
        self.events.append("timeout")

    async def _on_core_error(self, req: CoreProcessRequest, exc: BaseException) -> None:
        self.events.append("error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_effect", "post_error", "expected_tail"),
    [
        (None, False, ["unbind", "post"]),
        (TimeoutError("late"), False, ["unbind", "timeout"]),
        (RuntimeError("core failed"), False, ["unbind", "error"]),
        (None, True, ["unbind", "post", "error"]),
    ],
)
async def test_event_bus_unbinds_on_success_timeout_and_core_or_post_error(
    side_effect: BaseException | None,
    post_error: bool,
    expected_tail: list[str],
) -> None:
    process = AsyncMock(return_value=_Result())
    if side_effect is not None:
        process.side_effect = side_effect
    adapter = _PipelineHarness(process, post_error=post_error)
    req = CoreProcessRequest("hello", "user", "test", "openid")

    await adapter._process_with_core(req)

    assert adapter.events[:3] == ["ack", "session", "bind"]
    assert adapter.events[-len(expected_tail) :] == expected_tail
    assert adapter.events.count("unbind") == 1


@pytest.mark.asyncio
async def test_qq_group_handler_passes_real_group_boundary_to_core() -> None:
    bot = _qq_bot()
    bot._group_locks = {"group-a": asyncio.Lock()}
    bot.nudge_engine = None
    bot._extract_group_message_input = AsyncMock(
        return_value=("hello", [], "", "alice-openid", "qq_alice-openid")
    )
    bot._identify_group_master = lambda _openid: True
    bot._handle_quick_commands = AsyncMock(return_value=False)
    bot._run_message_pipeline = AsyncMock()
    message = SimpleNamespace(group_openid="group-a")

    await bot._handle_group_at_message(message, "group-a")

    kwargs = bot._run_message_pipeline.await_args.kwargs
    assert kwargs["session_id"] == "qq_group:group-a"


def test_wechat_uses_bound_ttl_cache_instances_for_all_three_maps() -> None:
    bot = _wechat_bot()

    assert isinstance(bot._ctx_cache, TTLCache)
    assert bot._ctx_cache.values is bot._ctx_by_user
    assert bot._ctx_cache.stamps is bot._ctx_by_user_ts
    assert isinstance(bot._user_locks_cache, TTLCache)
    assert bot._user_locks_cache.values is bot._user_locks
    assert bot._user_locks_cache.stamps is bot._user_locks_ts
    assert isinstance(bot._last_status_cache, TTLCache)
    assert bot._last_status_cache.values is bot._last_status_by_user
    assert bot._last_status_cache.stamps is bot._last_status_by_user_ts


def test_ttl_cache_expires_caps_and_keeps_bound_dicts_synchronized() -> None:
    values: dict[str, str] = {}
    stamps: dict[str, float] = {}
    cache = TTLCache(values, stamps, ttl=100, max_size=2)

    with patch("channel_adapter_base.time.time", side_effect=[0, 0, 5, 5, 10, 10]):
        cache.set("old", "old-value")
        cache.set("middle", "middle-value")
        cache.set("new", "new-value")

    assert values == {"middle": "middle-value", "new": "new-value"}
    assert stamps == {"middle": 5, "new": 10}

    cache.ttl = 5
    with patch("channel_adapter_base.time.time", return_value=20):
        cache.prune()

    assert values == {}
    assert stamps == {}


def test_user_lock_prunes_expired_current_key_before_lookup() -> None:
    bot = _wechat_bot()
    now = time.time()
    old_lock = asyncio.Lock()
    bot._user_locks["target"] = old_lock
    bot._user_locks_ts["target"] = now - bot._USER_LOCK_TTL - 1
    for index in range(128):
        key = f"user-{index}"
        bot._user_locks[key] = asyncio.Lock()
        bot._user_locks_ts[key] = now

    new_lock = bot._user_lock("target")

    assert new_lock is not old_lock
    assert bot._user_locks["target"] is new_lock
    assert bot._user_locks_ts["target"] >= now


def test_user_lock_prunes_single_expired_current_key_before_lookup() -> None:
    bot = _wechat_bot()
    now = time.time()
    old_lock = asyncio.Lock()
    bot._user_locks["target"] = old_lock
    bot._user_locks_ts["target"] = now - bot._USER_LOCK_TTL - 1

    new_lock = bot._user_lock("target")

    assert new_lock is not old_lock
    assert bot._user_locks == {"target": new_lock}
    assert bot._user_locks_ts["target"] >= now


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("uncertain"), False])
async def test_paced_helper_passes_original_failure_and_index(failure: Any) -> None:
    adapter = ChannelAdapterBase()
    seen: list[tuple[int, Any]] = []
    calls = 0

    async def send_one(segment: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return True

    async def on_failure(index: int, original: Any) -> None:
        seen.append((index, original))

    with patch("channel_adapter_base.asyncio.sleep", AsyncMock()):
        ok = await adapter._send_segments_paced(
            ["one", "two", "three"],
            send_one,
            on_failure=on_failure,
            log_prefix="test",
        )

    assert ok is False
    assert seen == [(1, failure)]
    if isinstance(failure, BaseException):
        assert seen[0][1] is failure


@pytest.mark.asyncio
async def test_paced_helper_treats_none_as_original_failure() -> None:
    adapter = ChannelAdapterBase()
    seen: list[tuple[int, Any]] = []
    sent: list[str] = []

    async def send_one(segment: str) -> bool:
        sent.append(segment)
        return None  # type: ignore[return-value]

    async def on_failure(index: int, original: Any) -> None:
        seen.append((index, original))

    ok = await adapter._send_segments_paced(
        ["one", "two"],
        send_one,
        on_failure=on_failure,
        log_prefix="test",
    )

    assert ok is False
    assert seen == [(0, None)]
    assert sent == ["one"]


@pytest.mark.asyncio
async def test_paced_helper_propagates_failure_callback_cancellation() -> None:
    adapter = ChannelAdapterBase()

    async def send_one(segment: str) -> bool:
        return False

    async def on_failure(index: int, original: Any) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await adapter._send_segments_paced(
            ["one"],
            send_one,
            on_failure=on_failure,
            log_prefix="test",
        )


async def _exercise_qq_recovery(failure: Any) -> tuple[list[str], list[str]]:
    bot = _qq_bot()
    segments = ["first", "current-", "界" * 6000]
    attempts: list[str] = []
    recovered: list[str] = []

    bot._split_text_for_streaming = lambda text, chunk_size=300: segments
    bot._cap_stream_segments = lambda items, *args: items

    async def send_segment(
        message: Any,
        text: str,
        *,
        passive: bool,
        is_group: bool,
        log_key: str,
    ) -> bool:
        attempts.append(text)
        if len(attempts) == 2:
            if isinstance(failure, BaseException):
                raise failure
            return failure
        recovered.append(text)
        return True

    bot._send_stream_segment = send_segment
    with patch("channel_adapter_base.asyncio.sleep", AsyncMock()):
        await bot._send_streaming_reply(_Message(), "ignored")
    return attempts, recovered


@pytest.mark.asyncio
async def test_qq_timeout_recovery_skips_uncertain_current_and_resplits_bytes() -> None:
    attempts, recovered = await _exercise_qq_recovery(TimeoutError("uncertain"))

    assert attempts[:2] == ["first", "current-"]
    assert "current-" not in "".join(attempts[2:])
    assert "".join(attempts[2:]) == "界" * 6000
    assert all(len(piece.encode("utf-8")) <= 7800 for piece in attempts[2:])
    assert recovered[0] == "first"


@pytest.mark.asyncio
async def test_qq_false_recovery_includes_current_and_resplits_bytes() -> None:
    attempts, _ = await _exercise_qq_recovery(False)

    assert attempts[:2] == ["first", "current-"]
    assert "".join(attempts[2:]) == "current-" + "界" * 6000
    assert all(len(piece.encode("utf-8")) <= 7800 for piece in attempts[2:])


@pytest.mark.asyncio
async def test_wechat_recovery_includes_current_resplits_bytes_and_stops_on_failure() -> None:
    reply_segments = ["first", "界" * 6000, "never-direct"]
    core = SimpleNamespace(
        process=AsyncMock(return_value=_Result("long reply")),
        get_session=AsyncMock(return_value={"id": "sid"}),
        create_session=AsyncMock(),
    )
    bot = _wechat_bot(core)
    bot._split_text_for_streaming = lambda text, chunk_size=300: reply_segments
    bot._cap_stream_segments = lambda items, *args: items
    sent: list[str] = []

    async def send_message(
        content: str,
        msg_type: str = "text",
        to_user_id: str = "",
        context_token: str = "",
    ) -> bool:
        sent.append(content)
        if content == reply_segments[1]:
            return False
        return content != "never-direct"

    bot.send_message = send_message
    with patch("channel_adapter_base.asyncio.sleep", AsyncMock()):
        await bot._handle_text_message("hello", "wx-openid", "ctx")

    recovered = sent[3:]
    assert "".join(recovered) == reply_segments[1] + reply_segments[2]
    assert all(len(piece.encode("utf-8")) <= 7800 for piece in recovered)
    assert "never-direct" not in sent[:3]
