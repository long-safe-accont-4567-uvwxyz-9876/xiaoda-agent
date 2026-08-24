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
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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
    fake = FakeAsyncClient([{"upload_param": "UP123", "upload_full_url": "https://cdn.example/upload"}])
    client = ILinkClient(bot_token="tok", client=fake)
    key = bytes(range(16))
    upload_param, upload_full_url, filekey, encrypted = asyncio.run(
        client._get_upload_url("user@im.wechat", PNG_BYTES, key)
    )
    assert upload_param == "UP123"
    assert upload_full_url == "https://cdn.example/upload"
    assert len(filekey) == 32  # 16 字节 hex
    assert len(encrypted) == ((len(PNG_BYTES) + 1) // 16 + 1) * 16

    req = fake.calls[0]["json"]
    assert req["media_type"] == 1
    assert req["no_need_thumb"] is True
    assert req["rawsize"] == len(PNG_BYTES)
    assert req["aeskey"] == key.hex()
    assert "/ilink/bot/getuploadurl" in fake.calls[0]["url"]


def test_upload_to_cdn_returns_encrypted_param():
    """CDN 上传返回 x-encrypted-param 头；无 upload_full_url 时回退拼接 URL。"""
    fake = FakeAsyncClient([])
    client = ILinkClient(bot_token="tok", client=fake)
    param = asyncio.run(client._upload_to_cdn("UP123", "", "f" * 32, b"\x00" * 16))
    assert param == "encrypted-param-value"
    assert "/upload" in fake.calls[0]["url"]
    assert "encrypted_query_param=UP123" in fake.calls[0]["url"]
    assert fake.calls[0]["content"] == b"\x00" * 16


def test_upload_to_cdn_prefers_upload_full_url():
    """服务端返回 upload_full_url 时，CDN 上传直接使用该 URL（对齐官方 SDK）。"""
    fake = FakeAsyncClient([])
    client = ILinkClient(bot_token="tok", client=fake)
    param = asyncio.run(
        client._upload_to_cdn("UP123", "https://cdn.srv/c2c/upload?x=1", "f" * 32, b"\x00" * 16)
    )
    assert param == "encrypted-param-value"
    assert fake.calls[0]["url"] == "https://cdn.srv/c2c/upload?x=1"
    assert fake.calls[0]["content"] == b"\x00" * 16


def test_send_media_message_sends_text_and_image_merged(tmp_path):
    """图文分开：文本与图片分别作为独立消息发送（item_list 仅含单项）。"""
    img = tmp_path / "sticker.png"
    img.write_bytes(PNG_BYTES)
    # 顺序：getuploadurl → CDN upload → sendmessage(text) → sendmessage(image)
    fake = FakeAsyncClient([{"upload_param": "UP123"}, {"ret": 0}, {"ret": 0}])
    client = ILinkClient(bot_token="tok", client=fake)
    result = asyncio.run(
        client.send_media_message("user@im.wechat", "ctx_tok", "你好～", str(img))
    )
    assert result == {"ret": 0}

    # 4 次调用：getuploadurl, CDN upload, sendmessage(text), sendmessage(image)
    assert len(fake.calls) == 4
    assert "/ilink/bot/getuploadurl" in fake.calls[0]["url"]
    assert "/upload" in fake.calls[1]["url"]

    # 文本消息：item_list 仅含单项文本
    text_msg = fake.calls[2]["json"]["msg"]
    assert text_msg["from_user_id"] == ""
    assert text_msg["client_id"]
    assert text_msg["message_type"] == 2
    assert text_msg["message_state"] == 2
    assert len(text_msg["item_list"]) == 1
    assert text_msg["item_list"][0]["type"] == 1
    assert text_msg["item_list"][0]["text_item"]["text"] == "你好～"

    # 图片消息：item_list 仅含单项 image_item
    img_msg = fake.calls[3]["json"]["msg"]
    assert len(img_msg["item_list"]) == 1
    items = img_msg["item_list"]
    assert items[0]["type"] == 2
    image_item = items[0]["image_item"]
    # 出站 image_item 不含 aeskey（官方仅在 media.aes_key 携带）
    assert "aeskey" not in image_item
    assert image_item["mid_size"] > 0

    media = image_item["media"]
    assert media["encrypt_query_param"] == "encrypted-param-value"
    # aes_key 必须是 aeskey hex 的 ASCII 字节 base64 编码（微信协议要求）
    from base64 import b64decode
    decoded_hex = b64decode(media["aes_key"]).decode("ascii")
    assert len(bytes.fromhex(decoded_hex)) == 16
    assert media["encrypt_type"] == 1


def test_send_media_message_missing_file_raises(tmp_path):
    """图片不存在时抛异常（由调用方回退纯文本）。"""
    fake = FakeAsyncClient([{"ret": 0}])
    client = ILinkClient(bot_token="tok", client=fake)
    try:
        asyncio.run(client.send_media_message("u", "t", "hi", "/nonexistent.png"))
        assert False, "应抛出 FileNotFoundError"
    except FileNotFoundError:
        pass
