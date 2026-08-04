"""ilink_client.send_message 回归测试

锁定修复 ret=-2 (prepare failed) 的两个关键点：
1. 请求体必须包含官方 SDK 的隐藏必填字段 from_user_id（空串）和 client_id（唯一）
2. ret=-2 时直接抛异常，不再做无效的 tokenless 降级重试
   （context_token 是消息路由必需字段，去掉它服务端仍返回 ret=-2）

参考协议：https://www.wechatbot.dev/en/protocol  (ret:-2 = Parameter error)
"""
import asyncio
import json
import re

import pytest

from ilink_client import ILinkClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeAsyncClient:
    """记录 post 调用并按序返回预设响应的假 httpx.AsyncClient。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        payload = self._payloads.pop(0) if self._payloads else {"ret": 0}
        return FakeResponse(payload)

    async def aclose(self):
        pass


def test_send_message_includes_hidden_required_fields():
    """send_message 请求体必须包含 from_user_id（空串）和 client_id（唯一）。"""
    fake = FakeAsyncClient([{"ret": 0}])
    client = ILinkClient(client=fake)
    asyncio.run(client.send_message("user@im.wechat", "ctx_tok", "你好"))

    assert len(fake.calls) == 1
    msg = fake.calls[0]["json"]["msg"]
    # 隐藏必填字段（缺失会导致服务端 ret=-2 prepare failed 或静默丢弃）
    assert msg["from_user_id"] == ""
    assert msg["client_id"] and isinstance(msg["client_id"], str)
    assert msg["message_type"] == 2  # BOT
    assert msg["message_state"] == 2  # FINISH
    assert msg["context_token"] == "ctx_tok"
    assert msg["to_user_id"] == "user@im.wechat"
    assert msg["item_list"][0]["text_item"]["text"] == "你好"
    # base_info 仍由 _build_body 注入
    assert fake.calls[0]["json"]["base_info"]["channel_version"]


def test_send_message_client_id_unique_per_call():
    """每次 send_message 的 client_id 必须不同（服务端去重依赖此字段）。"""
    fake = FakeAsyncClient([{"ret": 0}, {"ret": 0}])
    client = ILinkClient(client=fake)
    asyncio.run(client.send_message("u", "t", "a"))
    asyncio.run(client.send_message("u", "t", "b"))

    ids = [c["json"]["msg"]["client_id"] for c in fake.calls]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    # client_id 形如 bot-<hex16>
    assert all(re.fullmatch(r"bot-[0-9a-f]{16}", cid) for cid in ids)


def test_send_message_ret_minus_2_raises_no_tokenless_retry():
    """ret=-2（context_token 过期/无效）时直接抛异常，不做 tokenless 降级重试。

    旧逻辑会清空 context_token 重试一次，但 context_token 是消息路由必需字段，
    去掉它服务端仍返回 ret=-2，重试必然失败且掩盖根因。
    """
    fake = FakeAsyncClient([{"ret": -2, "errmsg": "prepare failed"}])
    client = ILinkClient(client=fake)
    with pytest.raises(RuntimeError, match="ret=-2"):
        asyncio.run(client.send_message("u", "stale_tok", "hi"))

    # 只发一次请求（不再 tokenless 重试）
    assert len(fake.calls) == 1
    # 那次请求仍携带原 context_token（未降级为空）
    assert fake.calls[0]["json"]["msg"]["context_token"] == "stale_tok"


def test_send_message_ok_returns_ret_zero():
    """正常发送返回 {"ret": 0}。"""
    fake = FakeAsyncClient([{"ret": 0}])
    client = ILinkClient(client=fake)
    result = asyncio.run(client.send_message("u", "t", "hi"))
    assert result == {"ret": 0}
