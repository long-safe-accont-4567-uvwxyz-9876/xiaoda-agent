"""TDD 测试：/media 静态目录 token 鉴权（VULN-29 + query token 移除）。

背景：app.mount("/media", StaticFiles(...)) 完全无鉴权，任何能访问端口的人可
下载 TTS 语音（含情感陪伴私密语音）、生成图片与用户上传文件。

修复演进：
- AuthStaticFiles 从 scope 提取凭据校验，无效 401；
- 安全加固：token 不再经 URL 传递（会泄露到访问日志/Referer/浏览器历史），
  ``?token=`` 查询参数一律拒绝（401，fail-closed）；web/routers/media.py
  返回的 audio_url/url 保持原样不再拼 token。

测试策略：
- fastapi TestClient 挂载最小 FastAPI 应用（仅 AuthStaticFiles mount），避免
  启动完整 server 的重状态；
- 有效/无效 token 用 monkeypatch 控制 _validate_token，另用真实
  _issue_token() 做一次端到端签发校验；
- _signed_media_url 与 gallery 端点做纯函数级单测。
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.media_auth import AuthStaticFiles
from web.routers import media as media_router


@pytest.fixture
def media_dir(tmp_path) -> Path:
    (tmp_path / "tts").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "tts" / "hello.wav"
    p.write_bytes(b"RIFFFAKE")
    return tmp_path


@pytest.fixture
def client(media_dir):
    app = FastAPI()
    app.mount("/media", AuthStaticFiles(directory=str(media_dir),
                                        follow_symlink=True), name="media")
    with TestClient(app) as c:
        yield c


# ── 无凭据 → 401 ──────────────────────────────────────────────────

def test_media_without_token_401(client):
    r = client.get("/media/tts/hello.wav")
    assert r.status_code == 401


# ── query ?token= 一律拒绝（安全修复：token 不得出现在 URL）───────

def test_media_query_token_rejected_even_if_valid(client, monkeypatch):
    """即使 token 本身有效，?token= 也必须 401（URL 凭据已移除）。"""
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    r = client.get("/media/tts/hello.wav?token=good")
    assert r.status_code == 401


def test_media_invalid_query_token_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    r = client.get("/media/tts/hello.wav?token=forged-token")
    assert r.status_code == 401


def test_media_validation_exception_fail_closed(client, monkeypatch):
    """_validate_token 抛异常也必须按无效处理（401），不能放行。"""
    def _boom(token: str) -> bool:
        raise RuntimeError("boom")
    monkeypatch.setattr("web.routers.auth._validate_token", _boom)
    r = client.get("/media/tts/hello.wav")
    assert r.status_code == 401


def test_media_empty_token_param_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: True)
    r = client.get("/media/tts/hello.wav?token=")
    assert r.status_code == 401


# ── cookie / Bearer 有效凭据 → 200 ────────────────────────────────

def test_media_valid_cookie_200(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    r = client.get("/media/tts/hello.wav",
                   headers={"Cookie": "x_media_token=good"})
    assert r.status_code == 200
    assert r.content == b"RIFFFAKE"
    # no-cache 行为保留（原 NoCacheMediaStaticFiles 语义）
    assert r.headers["Cache-Control"] == "no-cache, must-revalidate"


def test_media_invalid_cookie_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    r = client.get("/media/tts/hello.wav",
                   headers={"Cookie": "x_media_token=bad"})
    assert r.status_code == 401


def test_media_real_cookie_token_200(client):
    """登录下发的真实 token 通过 cookie 也能访问。"""
    from web.routers.auth import _issue_token
    token, _expiry = _issue_token()
    r = client.get("/media/tts/hello.wav",
                   headers={"Cookie": f"x_media_token={token}"})
    assert r.status_code == 200


def test_media_bearer_token_200(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    r = client.get("/media/tts/hello.wav",
                   headers={"Authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.content == b"RIFFFAKE"


def test_media_bearer_invalid_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    r = client.get("/media/tts/hello.wav",
                   headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_media_valid_bearer_missing_file_404(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: True)
    r = client.get("/media/tts/nope.wav",
                   headers={"Authorization": "Bearer good"})
    assert r.status_code == 404


def test_media_bearer_path_traversal_blocked(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: True)
    r = client.get("/media/tts/../../etc/passwd",
                   headers={"Authorization": "Bearer good"})
    assert r.status_code in (404, 401)


def test_media_real_issued_token_via_cookie_200(client):
    """端到端：真实 _issue_token 签发的 token 经 cookie 可通过静态目录鉴权。"""
    from web.routers.auth import _issue_token
    token, _expiry = _issue_token()
    r = client.get("/media/tts/hello.wav",
                   headers={"Cookie": f"x_media_token={token}"})
    assert r.status_code == 200


# ── web/routers/media.py 的 URL 不再拼 token ──────────────────────

class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


def test_signed_media_url_keeps_url_plain_with_bearer():
    """带 Authorization 头时 url 也保持原样（不再拼 ?token=）。"""
    req = _FakeRequest({"Authorization": "Bearer abc123"})
    assert media_router._signed_media_url(req, "/media/tts/x.wav") == \
        "/media/tts/x.wav"


def test_signed_media_url_without_auth_unchanged():
    req = _FakeRequest({})
    assert media_router._signed_media_url(req, "/media/tts/x.wav") == "/media/tts/x.wav"


def test_signed_media_url_non_bearer_unchanged():
    req = _FakeRequest({"Authorization": "Basic dXNlcjpwdw=="})
    assert media_router._signed_media_url(req, "/media/tts/x.wav") == "/media/tts/x.wav"


async def test_gallery_urls_stay_plain(tmp_path, monkeypatch):
    """gallery 返回的 url 必须保持原样（不带任何 token）。"""
    img_dir = tmp_path / "image"
    img_dir.mkdir(parents=True)
    (img_dir / "a.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(media_router, "MEDIA_ROOT", tmp_path)
    req = _FakeRequest({"Authorization": "Bearer tok123"})
    data = await media_router.gallery(req, type="image", page=0, limit=24)
    assert data.data and data.data[0]["url"] == "/media/image/a.png"


async def test_gallery_urls_without_token_stay_plain(tmp_path, monkeypatch):
    """无 Authorization 头时 url 同样保持原样。"""
    img_dir = tmp_path / "image"
    img_dir.mkdir(parents=True)
    (img_dir / "a.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(media_router, "MEDIA_ROOT", tmp_path)
    req = _FakeRequest({})
    data = await media_router.gallery(req, type="image", page=0, limit=24)
    assert data.data and data.data[0]["url"] == "/media/image/a.png"


# ── 贴纸端点凭据链（cookie → Bearer，query token 永不接受）──────────

class _FakeStickerRequest:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


@pytest.mark.asyncio
async def test_sticker_serves_with_media_cookie(monkeypatch, tmp_path):
    """裸 <img> 场景：x_media_token cookie 必须可通过（Path=/api/v1/agents）。"""

    import web.routers.agents as agents_router

    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    emo_dir = tmp_path / "happy"
    emo_dir.mkdir(parents=True)
    (emo_dir / "s.jpg").write_bytes(b"JPEGDATA")
    monkeypatch.setattr(agents_router, "_resolve_sticker_dir", lambda n, r: tmp_path)

    req = _FakeStickerRequest(cookies={"x_media_token": "good"})
    resp = await agents_router.serve_sticker("xiaoda", "s.jpg", req)
    assert resp.path.endswith("s.jpg")


@pytest.mark.asyncio
async def test_sticker_serves_with_bearer_header(monkeypatch, tmp_path):
    import web.routers.agents as agents_router

    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    emo_dir = tmp_path / "happy"
    emo_dir.mkdir(parents=True)
    (emo_dir / "s.jpg").write_bytes(b"JPEGDATA")
    monkeypatch.setattr(agents_router, "_resolve_sticker_dir", lambda n, r: tmp_path)

    req = _FakeStickerRequest(headers={"authorization": "Bearer good"})
    resp = await agents_router.serve_sticker("xiaoda", "s.jpg", req)
    assert resp.path.endswith("s.jpg")


@pytest.mark.asyncio
async def test_sticker_without_credentials_401(monkeypatch):
    from fastapi import HTTPException

    import web.routers.agents as agents_router

    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    with pytest.raises(HTTPException) as exc:
        await agents_router.serve_sticker(
            "xiaoda", "s.jpg", _FakeStickerRequest())
    assert exc.value.status_code == 401


def test_media_cookie_dual_path_issuance_and_clear():
    """登录/登出必须在 /media 与 /api/v1/agents 双路径下发/清除 cookie。"""

    class _Resp:
        def __init__(self):
            self.raw = ""

        def set_cookie(self, *args, **kwargs):
            self.raw += f"set {kwargs.get('path')}"

        def delete_cookie(self, name, path="/"):
            self.raw += f"del {path}"

    resp = _Resp()
    from web.routers.auth import clear_media_cookie, set_media_cookie
    set_media_cookie(resp, "tok", __import__("time").time() + 3600)
    assert "/media" in resp.raw and "/api/v1/agents" in resp.raw
    resp2 = _Resp()
    clear_media_cookie(resp2)
    assert "/media" in resp2.raw and "/api/v1/agents" in resp2.raw
