# 微信 ACK 与表情包系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让微信 Bot 拥有与 QQ 一致的 ACK（"收到啦，正在想～"）与表情包（文字+图片合并发送）能力。

**Architecture:** 在 `ilink_client.py` 协议层新增图片上传与图文消息发送（getuploadurl → AES-128-ECB → CDN 上传 → sendmessage 引用 image_item）；在 `wechat_bot_adapter.py` 补 ACK 发送时机与 `sticker_path` 图文合并回复，失败回退纯文本。

**Tech Stack:** Python 3.10+, httpx, cryptography (AES-128-ECB)。

## Global Constraints

- 复用现有 `_post`/`_build_headers`/`_client`，不重复造轮子。
- `aeskey`（getuploadurl 用）用 hex 字符串；`image_item.media.aes_key` 用 base64。
- 图片上传任一环节失败 → 回退纯文本，绝不阻塞用户回复。
- 会话过期（-14）沿用现有 `SessionExpiredError` 处理。
- 测试用 `pytest`，`asyncio.run()` 同步调用 async 函数。
- 复用 `cryptography` 库（requirements.txt 已含 `cryptography>=43.0.0`）。

---

### Task 1: ilink_client — AES 加密 + 上传原语

**Files:**
- Modify: `ilink_client.py`（新增方法 + 顶部 import）
- Test: `tests/test_ilink_send_media.py`（新建）

**Interfaces:**
- Produces:
  - `ILinkClient._aes_ecb_encrypt(data: bytes, key: bytes) -> bytes`（AES-128-ECB + PKCS7）
  - `ILinkClient._get_upload_url(to_user_id: str, raw_bytes: bytes, aes_key: bytes) -> tuple[str, str, bytes]`（返回 `(upload_param, filekey, encrypted_bytes)`）
  - `ILinkClient._upload_to_cdn(upload_param: str, filekey: str, encrypted: bytes) -> str`（返回 `x-encrypted-param`）

- [ ] **Step 1: 写失败测试**

写入 `tests/test_ilink_send_media.py`：

```python
"""ilink_client 图片上传/发送回归测试"""
import asyncio
import json

from ilink_client import ILinkClient

# 1x1 透明 PNG
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360010000050001d3428fbf0000000049454e44ae426082"
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeAsyncClient:
    """记录请求；getuploadurl/sendmessage 走 payloads，CDN /upload 返回 x-encrypted-param 头。"""

    def __init__(self, payloads, cdn_header="encrypted-param-value"):
        self._payloads = list(payloads)
        self.calls = []
        self.cdn_header = cdn_header

    async def post(self, url, json=None, headers=None, timeout=None, content=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "content": content})
        if "/upload" in url:
            return FakeResponse({}, headers={"x-encrypted-param": self.cdn_header})
        item = self._payloads.pop(0) if self._payloads else {"ret": 0}
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item if isinstance(item, dict) else {"ret": 0})

    async def aclose(self):
        pass


def test_aes_ecb_encrypt_roundtrip():
    """AES-128-ECB 加密后可用同一 key 解密还原。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = bytes(range(16))
    client = ILinkClient(bot_token="tok", client=FakeAsyncClient([]))
    ciphertext = client._aes_ecb_encrypt(PNG_BYTES, key)
    # 密文长度 = ceil((len+1)/16)*16
    assert len(ciphertext) == ((len(PNG_BYTES) + 1) // 16 + 1) * 16

    padder = padding.PKCS7(128).unpadder()
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = padder.update(dec.update(ciphertext) + dec.finalize()) + padder.finalize()
    assert plain == PNG_BYTES


def test_get_upload_url_requests_expected_fields():
    """getuploadurl 请求体含 media_type=1、no_need_thumb、rawsize、rawfilemd5、aeskey(hex)。"""
    fake = FakeAsyncClient([{"upload_param": "UP123"}])
    client = ILinkClient(bot_token="tok", client=fake)
    key = bytes(range(16))
    upload_param, filekey, encrypted = asyncio.run(
        client._get_upload_url("user@im.wechat", PNG_BYTES, key)
    )
    assert upload_param == "UP123"
    assert len(filekey) == 32  # 16 字节 hex
    assert len(encrypted) == ((len(PNG_BYTES) + 1) // 16 + 1) * 16

    req = fake.calls[0]["json"]
    assert req["media_type"] == 1
    assert req["no_need_thumb"] is True
    assert req["rawsize"] == len(PNG_BYTES)
    assert req["aeskey"] == key.hex()
    assert "/uploadurl" in fake.calls[0]["url"]


def test_upload_to_cdn_returns_encrypted_param():
    """CDN 上传返回 x-encrypted-param 头。"""
    fake = FakeAsyncClient([])
    client = ILinkClient(bot_token="tok", client=fake)
    param = asyncio.run(client._upload_to_cdn("UP123", "f" * 32, b"\x00" * 16))
    assert param == "encrypted-param-value"
    assert "/upload" in fake.calls[0]["url"]
    assert "encrypted_query_param=UP123" in fake.calls[0]["url"]
    assert fake.calls[0]["content"] == b"\x00" * 16
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ilink_send_media.py -v`
Expected: FAIL with `AttributeError: 'ILinkClient' object has no attribute '_aes_ecb_encrypt'`

- [ ] **Step 3: 实现最小代码**

在 `ilink_client.py` 顶部新增 import：

```python
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
```

在 `ILinkClient` 类内（`send_message` 方法之后）新增：

```python
    @staticmethod
    def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB + PKCS7 加密（iLink 媒体上传协议）。"""
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    async def _get_upload_url(
        self, to_user_id: str, raw_bytes: bytes, aes_key: bytes
    ) -> tuple[str, str, bytes]:
        """获取 CDN 上传参数并预加密，返回 (upload_param, filekey, encrypted)。

        Args:
            to_user_id: 目标用户 ID
            raw_bytes: 图片明文二进制
            aes_key: 随机 16 字节 AES 密钥

        Returns:
            (upload_param, filekey, encrypted): 上传参数、文件标识、加密后数据
        """
        encrypted = self._aes_ecb_encrypt(raw_bytes, aes_key)
        filekey = os.urandom(16).hex()
        data = {
            "filekey": filekey,
            "media_type": 1,  # IMAGE
            "to_user_id": to_user_id,
            "rawsize": len(raw_bytes),
            "rawfilemd5": hashlib.md5(raw_bytes).hexdigest(),
            "filesize": len(encrypted),
            "no_need_thumb": True,  # 表情包无需缩略图
            "aeskey": aes_key.hex(),
        }
        payload = await self._post("/ilink/bot/getuploadurl", data=data)
        upload_param = payload.get("upload_param", "")
        logger.info("ilink.get_upload_url.ok to={} upload_param_len={}", to_user_id, len(upload_param))
        return upload_param, filekey, encrypted

    async def _upload_to_cdn(self, upload_param: str, filekey: str, encrypted: bytes) -> str:
        """上传加密数据到 CDN，返回 x-encrypted-param（后续 sendmessage 引用）。"""
        url = (
            f"{ILINK_CDN_URL}/upload"
            f"?encrypted_query_param={upload_param}&filekey={filekey}"
        )
        try:
            response = await self._client.post(
                url,
                content=encrypted,
                headers={"Content-Type": "application/octet-stream"},
            )
        except httpx.HTTPError as e:
            logger.error("ilink.cdn_upload.http_error url={} error={}", url, str(e)[:200])
            raise
        if response.status_code != 200:
            logger.error(
                "ilink.cdn_upload.bad_status status={} body={}",
                response.status_code, response.text[:200],
            )
            raise RuntimeError(
                f"iLink CDN upload HTTP {response.status_code}: {response.text[:120]}"
            )
        param = response.headers.get("x-encrypted-param", "")
        logger.info("ilink.cdn_upload.ok param_len={}", len(param))
        return param
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ilink_send_media.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add ilink_client.py tests/test_ilink_send_media.py
git commit -m "feat(wechat): ilink_client 新增 AES 加密与 CDN 媒体上传原语"
```

---

### Task 2: ilink_client — send_media_message（文字+图片合并）

**Files:**
- Modify: `ilink_client.py`（新增 `send_media_message`）
- Test: `tests/test_ilink_send_media.py`（追加）

**Interfaces:**
- Consumes: `ILinkClient._get_upload_url`、`ILinkClient._upload_to_cdn`、`ILinkClient._post`（Task 1）
- Produces: `ILinkClient.send_media_message(to_user_id: str, context_token: str, text: str, image_path: str) -> dict`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ilink_send_media.py`：

```python
def test_send_media_message_sends_text_and_image_merged(tmp_path):
    """图文合并：一条消息的 item_list 同时含 text 和 image_item。"""
    img = tmp_path / "sticker.png"
    img.write_bytes(PNG_BYTES)
    # 顺序：getuploadurl → CDN upload → sendmessage
    fake = FakeAsyncClient([{"upload_param": "UP123"}, {"ret": 0}])
    client = ILinkClient(bot_token="tok", client=fake)
    result = asyncio.run(
        client.send_media_message("user@im.wechat", "ctx_tok", "你好～", str(img))
    )
    assert result == {"ret": 0}

    # 3 次调用：getuploadurl, CDN upload, sendmessage
    assert len(fake.calls) == 3
    assert "/uploadurl" in fake.calls[0]["url"]
    assert "/upload" in fake.calls[1]["url"]

    msg = fake.calls[2]["json"]["msg"]
    assert msg["from_user_id"] == ""
    assert msg["client_id"]
    assert msg["message_type"] == 2
    assert msg["message_state"] == 2
    items = msg["item_list"]
    assert items[0]["type"] == 1
    assert items[0]["text_item"]["text"] == "你好～"
    assert items[1]["type"] == 2
    media = items[1]["image_item"]["media"]
    assert media["encrypt_query_param"] == "encrypted-param-value"
    assert media["encrypt_type"] == 0


def test_send_media_message_missing_file_raises(tmp_path):
    """图片不存在时抛异常（由调用方回退纯文本）。"""
    fake = FakeAsyncClient([{"ret": 0}])
    client = ILinkClient(bot_token="tok", client=fake)
    try:
        asyncio.run(client.send_media_message("u", "t", "hi", "/nonexistent.png"))
        assert False, "应抛出 FileNotFoundError"
    except FileNotFoundError:
        pass
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ilink_send_media.py -v`
Expected: 新增 2 个 FAIL（`AttributeError: send_media_message`）

- [ ] **Step 3: 实现最小代码**

在 `ilink_client.py` 的 `send_message` 方法之后新增：

```python
    async def send_media_message(
        self, to_user_id: str, context_token: str, text: str, image_path: str
    ) -> dict:
        """发送文字+图片合并消息（表情包）。

        完整流程：getuploadurl → AES-128-ECB 加密 → CDN 上传 →
        sendmessage 的 item_list 同时含 text_item 与 image_item。

        Args:
            to_user_id: 接收方用户 ID
            context_token: 会话上下文 token
            text: 文本内容（可为空串，仅发图）
            image_path: 本地图片文件路径（表情包）

        Returns:
            字典包含:
                - ret (int): 返回码，0 表示成功

        Raises:
            FileNotFoundError: image_path 不存在
            SessionExpiredError: ret == -14
            httpx.HTTPError: 网络错误
            RuntimeError: 上传/发送失败（含 ret != 0）
        """
        with open(image_path, "rb") as f:
            raw_bytes = f.read()
        aes_key = os.urandom(16)
        upload_param, filekey, encrypted = await self._get_upload_url(
            to_user_id, raw_bytes, aes_key
        )
        encrypted_param = await self._upload_to_cdn(upload_param, filekey, encrypted)

        data = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bot-{uuid.uuid4().hex[:16]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {"type": 1, "text_item": {"text": text}},
                    {
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": encrypted_param,
                                "aes_key": base64.b64encode(aes_key).decode("ascii"),
                                "encrypt_type": 0,
                            }
                        },
                    },
                ],
            }
        }
        payload = await self._post("/ilink/bot/sendmessage", data=data)
        ret = payload.get("ret", RET_OK)
        logger.info(
            "ilink.send_media_message.ok to={} text_len={} img_len={} ret={}",
            to_user_id, len(text), len(raw_bytes), ret,
        )
        return {"ret": ret}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ilink_send_media.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add ilink_client.py tests/test_ilink_send_media.py
git commit -m "feat(wechat): ilink_client 新增 send_media_message 图文合并发送"
```

---

### Task 3: wechat_bot_adapter — ACK 发送

**Files:**
- Modify: `wechat_bot_adapter.py`（`_handle_text_message`）
- Test: `tests/test_wechat_ack_sticker.py`（新建）

**Interfaces:**
- Consumes: `emotion.emoji_config.get_ack_message(agent_name)`；`WeChatBotAdapter.send_message`（已有）
- Produces: 无（内部行为——ACK 在 process 前发送）

- [ ] **Step 1: 写失败测试**

写入 `tests/test_wechat_ack_sticker.py`：

```python
"""wechat_bot_adapter ACK 与表情包行为测试"""
import asyncio
from pathlib import Path

from wechat_bot_adapter import WeChatBotAdapter


class FakeResult:
    def __init__(self, reply, sticker_path=None):
        self.reply = reply
        self.sticker_path = sticker_path


class FakeCore:
    def __init__(self, result):
        self._result = result
        self.called = False

    async def process(self, text, user_id="", source="", user_openid=""):
        self.called = True
        return self._result


class FakeClient:
    def __init__(self):
        self.sent = []
        self.media_sent = []

    async def send_message(self, to_user_id, context_token, text):
        self.sent.append((to_user_id, context_token, text))
        return True

    async def send_media_message(self, to_user_id, context_token, text, image_path):
        self.media_sent.append((to_user_id, context_token, text, image_path))
        return {"ret": 0}


def _make_adapter(core, sticker_path=None):
    adapter = WeChatBotAdapter.__new__(WeChatBotAdapter)
    adapter._core = core
    adapter._ilink_client = FakeClient()
    adapter._last_from_user_id = "user@im.wechat"
    adapter._last_context_token = "ctx_tok"
    adapter._expired = False
    return adapter


def test_ack_sent_before_process():
    """process 前先发送 ACK。"""
    core = FakeCore(FakeResult("回复"))
    adapter = _make_adapter(core)
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    assert core.called
    # ACK 是第一条发送
    assert adapter._ilink_client.sent
    first_text = adapter._ilink_client.sent[0][2]
    assert "收到啦" in first_text or "正在想" in first_text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_wechat_ack_sticker.py -v`
Expected: FAIL（`_handle_text_message` 未发 ACK）

- [ ] **Step 3: 实现最小代码**

在 `wechat_bot_adapter.py` 的 `_handle_text_message` 中，`process()` 调用之前插入 ACK：

```python
        if self._core is None:
            logger.warning("wechat_bot.no_core text={}", text[:80])
            return

        user_id = f"wechat_{from_user_id}" if from_user_id else "wechat_unknown"
        logger.info("wechat_bot.text_msg user_id={} text={}", user_id, text[:80])

        # ACK：处理前立即发送"收到啦，正在想"（对齐 QQ 行为）
        try:
            from emotion.emoji_config import get_ack_message
            ack_text = get_ack_message("xiaoda")
            await self.send_message(
                ack_text,
                to_user_id=from_user_id,
                context_token=context_token,
            )
        except Exception as e:
            logger.warning("wechat_bot.ack_send_failed error={}", str(e)[:200])

        try:
            result = await asyncio.wait_for(
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_wechat_ack_sticker.py -v`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add wechat_bot_adapter.py tests/test_wechat_ack_sticker.py
git commit -m "feat(wechat): 微信 ACK 处理前发送（对齐 QQ）"
```

---

### Task 4: wechat_bot_adapter — 表情包图文合并 + send_sticker

**Files:**
- Modify: `wechat_bot_adapter.py`（`_handle_text_message` 回复处、`send_sticker`、新增 `send_media_message`）
- Test: `tests/test_wechat_ack_sticker.py`（追加）

**Interfaces:**
- Consumes: `ILinkClient.send_media_message`（Task 2）；`Path`（已 import）
- Produces: `WeChatBotAdapter.send_media_message(content: str, image_path: str, to_user_id: str = "", context_token: str = "") -> bool`；`WeChatBotAdapter.send_sticker(sticker_path: str) -> bool`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wechat_ack_sticker.py`：

```python
def test_reply_with_sticker_sends_merged(tmp_path):
    """有 sticker_path 时走图文合并，且 ACK 单独发。"""
    img = tmp_path / "s.png"
    img.write_bytes(b"fake-png-bytes")
    core = FakeCore(FakeResult("回复", sticker_path=str(img)))
    adapter = _make_adapter(core)
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    # ACK 单独文本发送
    assert adapter._ilink_client.sent
    # 图文合并发送
    assert adapter._ilink_client.media_sent
    _, _, text, path = adapter._ilink_client.media_sent[0]
    assert text == "回复"
    assert path == str(img)


def test_reply_without_sticker_sends_text(tmp_path):
    """无 sticker_path 时纯文本回复。"""
    core = FakeCore(FakeResult("回复"))
    adapter = _make_adapter(core)
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    # 真实文本回复（非 ACK）
    assert len(adapter._ilink_client.sent) == 2
    assert adapter._ilink_client.sent[1][2] == "回复"
    assert not adapter._ilink_client.media_sent


def test_reply_sticker_failure_falls_back_to_text(tmp_path):
    """图文合并失败时回退纯文本，不阻塞回复。"""
    img = tmp_path / "s.png"
    img.write_bytes(b"fake-png-bytes")
    core = FakeCore(FakeResult("回复", sticker_path=str(img)))
    adapter = _make_adapter(core)
    # 让 send_media_message 抛异常
    async def boom(*a, **k):
        raise RuntimeError("upload failed")
    adapter._ilink_client.send_media_message = boom
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    # 回退纯文本（最后一条是文本回复）
    assert adapter._ilink_client.sent[-1][2] == "回复"


def test_send_sticker_delegates_to_media(tmp_path):
    """send_sticker 只发图（text 为空）。"""
    img = tmp_path / "s.png"
    img.write_bytes(b"fake-png-bytes")
    adapter = _make_adapter(FakeCore(None))
    adapter._last_from_user_id = "user@im.wechat"
    adapter._last_context_token = "ctx_tok"
    ok = asyncio.run(adapter.send_sticker(str(img)))
    assert ok is True
    assert len(adapter._ilink_client.media_sent) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_wechat_ack_sticker.py -v`
Expected: 新增 4 个 FAIL（无 `send_media_message`、回复未走图文合并）

- [ ] **Step 3: 实现最小代码**

在 `wechat_bot_adapter.py` 的 `_handle_text_message` 回复处（`result` 获取后）替换为：

```python
        reply = getattr(result, "reply", "") or ""
        if not reply:
            return
        sticker_path = getattr(result, "sticker_path", None) or ""
        # 有 emotion 表情包 → 图文合并发送；失败回退纯文本
        if sticker_path and Path(sticker_path).exists():
            try:
                await self.send_media_message(
                    reply,
                    sticker_path,
                    to_user_id=from_user_id,
                    context_token=context_token,
                )
                return
            except Exception as e:
                logger.warning(
                    "wechat_bot.sticker_send_failed fallback_to_text error={}",
                    str(e)[:200],
                )
        await self.send_message(
            reply,
            to_user_id=from_user_id,
            context_token=context_token,
        )
```

在 `send_message` 方法之后新增 `send_media_message`，并替换 `send_sticker` 空实现：

```python
    async def send_media_message(
        self,
        content: str,
        image_path: str,
        to_user_id: str = "",
        context_token: str = "",
    ) -> bool:
        """发送文字+图片合并消息（表情包）。

        Args:
            content: 文本内容（可为空串，仅发图）
            image_path: 本地图片路径
            to_user_id: 目标用户（为空用缓存）
            context_token: 会话 token（为空用缓存）

        Returns:
            是否发送成功
        """
        if self._ilink_client is None:
            logger.warning("wechat_bot.send_media_no_client content={}", content[:40])
            return False
        target_user = to_user_id or self._last_from_user_id
        target_token = context_token or self._last_context_token
        if not target_user:
            logger.warning("wechat_bot.send_media_no_user_id")
            return False
        try:
            result = await self._ilink_client.send_media_message(
                target_user, target_token, content, image_path
            )
            return result.get("ret", 0) == 0
        except SessionExpiredError:
            logger.warning("wechat_bot.send_media_session_expired")
            self._expired = True
            self._connected = False
            self._clear_credentials()
            return False
        except Exception as e:
            logger.error(
                "wechat_bot.send_media_error error={} type={}",
                str(e)[:200], type(e).__name__,
            )
            return False

    async def send_sticker(self, sticker_path: str) -> bool:
        """发送微信表情包（仅图片，无文字）。

        Args:
            sticker_path: 表情包文件路径

        Returns:
            是否发送成功
        """
        return await self.send_media_message("", sticker_path)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_wechat_ack_sticker.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add wechat_bot_adapter.py tests/test_wechat_ack_sticker.py
git commit -m "feat(wechat): 微信表情包图文合并发送 + send_sticker 实现"
```

---

### Task 5: 全量回归

**Files:**
- 无新文件

- [ ] **Step 1: 运行全部相关测试**

Run: `python -m pytest tests/test_ilink_send_message.py tests/test_ilink_send_media.py tests/test_wechat_ack_sticker.py tests/test_wechat_bot_skeleton.py -v`
Expected: 全部 PASS（无回归）

- [ ] **Step 2: 验证后端语法**

Run: `python -c "import ast; ast.parse(open('ilink_client.py').read()); ast.parse(open('wechat_bot_adapter.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "test(wechat): 微信 ACK 与表情包全量回归通过"
```