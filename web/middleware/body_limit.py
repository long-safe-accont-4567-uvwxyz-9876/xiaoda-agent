"""全局请求体大小上限中间件（2026-08-26 web 审计 P1 治本）。

威胁模型：壁纸上传端点曾先整包 base64 解码后查业务限额--认证用户提交数百 MB
JSON body 会把弱设备（4GB ARM）内存打爆。端点级限额只保护"解码后的值"，
本中间件在传输层先行截断，漏配端点同样受默认保护：

- 带 Content-Length：直接 413，不读任何字节；
- chunked/无长度：包装 receive 计数，超限后回 http.disconnect 使读取短路，
  端点级限额随后拒绝（分层防御，不依赖本中间件单点）。

上限取值：合法最大 body 为 50MB 视频壁纸的 data URL（base64 膨胀 4/3 ≈
67MB + JSON 信封），故默认 80MB；/ws 握手无 body 不受影响。
"""
from __future__ import annotations

import json
import os

from utils.common import safe_int

DEFAULT_MAX_BODY_BYTES = 80 * 1024 * 1024
_MAX_BODY_BYTES = max(
    1024 * 1024,
    safe_int(os.getenv("WEBUI_MAX_BODY_MB", "80"), 80) * 1024 * 1024,
)


class BodySizeLimitMiddleware:
    """纯 ASGI 中间件：Content-Length 预检 + chunked 计数短路。"""

    def __init__(self, app, max_body_bytes: int = _MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        length: int | None = None
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = None
                break

        if length is not None:
            if length > self.max_body_bytes:
                body = json.dumps(
                    {"detail": f"请求体过大（上限 {self.max_body_bytes // (1024 * 1024)}MB）"},
                    ensure_ascii=False,
                ).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
            await self.app(scope, receive, send)
            return

        # chunked 传输：按消息计数，超限回 disconnect 短路读取
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_body_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)
