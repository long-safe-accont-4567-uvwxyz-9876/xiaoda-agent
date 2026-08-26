"""媒体静态目录鉴权：/media 下的 TTS 音频、生成图片、用户上传等敏感文件。

背景：web/server.py 原先 `app.mount("/media", NoCacheMediaStaticFiles(...))` 完全
无鉴权，任何能访问端口的人可下载 TTS 音频（含情感陪伴场景的私密语音）、生成
图片与用户上传文件。

方案：StaticFiles 子类在 get_response 入口提取凭据，调用
web.routers.auth._validate_token 校验；无效返回 401 JSONResponse，有效则透传
给父类。保留 follow_symlink（媒体符号链接到外置盘）与 no-cache 响应头
（继承原 NoCacheMediaStaticFiles 行为）。

凭据来源按序取两种之一（裸 <audio>/<img> 标签无法携带 Authorization 头，
故必须支持 cookie；token 一律不通过 URL 传递，避免出现在日志/Referer 中）：
    1. cookie ``x_media_token``（登录时下发，Path=/media，HttpOnly）
    2. Authorization: Bearer <token>
"""
from __future__ import annotations

from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles


def _token_from_scope(scope: dict) -> str:
    """从 ASGI scope 提取媒体访问凭据（cookie → Bearer）。不接受 query token。"""
    headers = scope.get("headers") or []
    # 1. cookie x_media_token
    try:
        from web.routers.auth import MEDIA_COOKIE_NAME
        cookie_name = MEDIA_COOKIE_NAME.encode("latin-1")
    except (ImportError, AttributeError):
        cookie_name = b"x_media_token"
    for name, value in headers:
        if name != b"cookie":
            continue
        try:
            cookie_header = value.decode("latin-1")
        except (UnicodeDecodeError, ValueError):
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
        except (UnicodeDecodeError, ValueError):
            continue
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if token:
                return token
    return ""


def _validate(token: str) -> bool:
    """容错校验 token：_validate_token 内部异常一律视为无效（fail-closed）。"""
    if not token:
        return False
    try:
        from web.routers.auth import _validate_token
        return bool(_validate_token(token))
    except (ImportError, ValueError, RuntimeError, OSError):
        return False


class AuthStaticFiles(StaticFiles):
    """带鉴权（cookie/Bearer）+ no-cache 头的媒体静态文件服务。"""

    async def get_response(self, path: str, scope: dict) -> Response:
        # 壁纸为装饰性公开资源（public-wallpaper 端点供登录页无鉴权展示），
        # 不做 token 校验；其余 /media 内容（TTS 音频/生成图/上传）仍须鉴权。
        if path.startswith("wallpapers/"):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            # HTML 动画壁纸：响应级 CSP 沙箱。直接导航访问时页面不带沙箱属性，
            # 若以同源执行可携带 cookie 调用本站 API（CSRF 面）；iframe 嵌入时
            # 与 sandbox="allow-scripts" 叠加，粒子/时钟动画不受影响。
            if path.lower().endswith((".html", ".htm")):
                resp.headers["Content-Security-Policy"] = "sandbox allow-scripts"
                resp.headers["X-Content-Type-Options"] = "nosniff"
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
