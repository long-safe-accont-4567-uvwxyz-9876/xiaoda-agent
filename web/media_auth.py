"""媒体静态目录鉴权：/media 下的 TTS 音频、生成图片、用户上传等敏感文件。

背景：web/server.py 原先 `app.mount("/media", NoCacheMediaStaticFiles(...))` 完全
无鉴权，任何能访问端口的人可下载 TTS 音频（含情感陪伴场景的私密语音）、生成
图片与用户上传文件。

方案：StaticFiles 子类在 get_response 入口提取凭据，调用
web.routers.auth._validate_token 校验；无效返回 401 JSONResponse，有效则透传
给父类。保留 follow_symlink（媒体符号链接到外置盘）与 no-cache 响应头
（继承原 NoCacheMediaStaticFiles 行为）。

VULN-29 扩展：凭据来源按序取三种之一（裸 <audio>/<img> 标签无法携带
Authorization 头，故必须支持 cookie）：
    1. cookie ``x_media_token``（登录时下发，Path=/media，HttpOnly）
    2. Authorization: Bearer <token>
    3. ``?token=`` 查询参数
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles


def _token_from_scope(scope: dict) -> str:
    """从 ASGI scope 提取媒体访问凭据（cookie → Bearer → query token）。"""
    headers = scope.get("headers") or []
    # 1. cookie x_media_token
    try:
        from web.routers.auth import MEDIA_COOKIE_NAME
        cookie_name = MEDIA_COOKIE_NAME.encode("latin-1")
    except Exception:
        cookie_name = b"x_media_token"
    for name, value in headers:
        if name != b"cookie":
            continue
        try:
            cookie_header = value.decode("latin-1")
        except Exception:
            continue
        for part in cookie_header.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip().encode("latin-1") == cookie_name:
                    token = v.strip()
                    if token:
                        return token
    # 2. Authorization: Bearer
    for name, value in headers:
        if name != b"authorization":
            continue
        try:
            auth = value.decode("latin-1")
        except Exception:
            continue
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if token:
                return token
    # 3. query string ?token=
    raw = scope.get("query_string") or b""
    try:
        qs = raw.decode("latin-1")
        params = parse_qs(qs)
    except Exception:
        return ""
    tokens = params.get("token") or []
    return tokens[0] if tokens else ""


def _validate(token: str) -> bool:
    """容错校验 token：_validate_token 内部异常一律视为无效（fail-closed）。"""
    if not token:
        return False
    try:
        from web.routers.auth import _validate_token
        return bool(_validate_token(token))
    except Exception:
        return False


class AuthStaticFiles(StaticFiles):
    """带鉴权（cookie/Bearer/query token）+ no-cache 头的媒体静态文件服务。"""

    async def get_response(self, path: str, scope: dict) -> Response:
        # 壁纸为装饰性公开资源（public-wallpaper 端点供登录页无鉴权展示），
        # 不做 token 校验；其余 /media 内容（TTS 音频/生成图/上传）仍须鉴权。
        if path.startswith("wallpapers/"):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp
        token = _token_from_scope(scope)
        if not _validate(token):
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp
