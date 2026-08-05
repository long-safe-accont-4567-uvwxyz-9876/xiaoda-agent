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

