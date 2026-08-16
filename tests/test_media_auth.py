"""TDD 测试：/media 静态目录 token 鉴权（VULN-29）。

背景：app.mount("/media", StaticFiles(...)) 完全无鉴权，任何能访问端口的人可
下载 TTS 语音（含情感陪伴私密语音）、生成图片与用户上传文件。

修复：AuthStaticFiles 从 scope 的 query_string 提取 token 参数校验，无效 401；
web/routers/media.py 返回的 audio_url/url 由后端拼上当前请求的 token。

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


# ── 无 token / 无效 token → 401 ───────────────────────────────────

def test_media_without_token_401(client):
    r = client.get("/media/tts/hello.wav")
    assert r.status_code == 401


def test_media_invalid_token_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    r = client.get("/media/tts/hello.wav?token=forged-token")
    assert r.status_code == 401


def test_media_validation_exception_fail_closed(client, monkeypatch):
    """_validate_token 抛异常也必须按无效处理（401），不能放行。"""
    def _boom(token: str) -> bool:
        raise RuntimeError("boom")
    monkeypatch.setattr("web.routers.auth._validate_token", _boom)
    r = client.get("/media/tts/hello.wav?token=x")
    assert r.status_code == 401


def test_media_empty_token_param_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    r = client.get("/media/tts/hello.wav?token=")
    assert r.status_code == 401


# ── 有效 token → 200（透传 + no-cache 头保留）─────────────────────

def test_media_valid_token_200(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    r = client.get("/media/tts/hello.wav?token=good")
    assert r.status_code == 200
    assert r.content == b"RIFFFAKE"
    # no-cache 行为保留（原 NoCacheMediaStaticFiles 语义）
    assert r.headers["Cache-Control"] == "no-cache, must-revalidate"


def test_media_valid_token_missing_file_404(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: True)
    r = client.get("/media/tts/nope.wav?token=good")
    assert r.status_code == 404


def test_media_valid_token_path_traversal_blocked(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: True)
    r = client.get("/media/tts/../../etc/passwd?token=good")
    assert r.status_code in (404, 401)


def test_media_real_issued_token_200(client):
    """端到端：真实 _issue_token 签发的 token 可通过静态目录鉴权。"""
    from web.routers.auth import _issue_token
    token, _expiry = _issue_token()
    r = client.get(f"/media/tts/hello.wav?token={token}")
    assert r.status_code == 200


# ── cookie / Bearer 凭据（AuthStaticFiles 同样接受）────────────────

def test_media_valid_cookie_200(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: t == "good")
    r = client.get("/media/tts/hello.wav",
                   headers={"Cookie": "x_media_token=good"})
    assert r.status_code == 200
    assert r.content == b"RIFFFAKE"


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


def test_media_bearer_invalid_401(client, monkeypatch):
    monkeypatch.setattr("web.routers.auth._validate_token", lambda t: False)
    r = client.get("/media/tts/hello.wav",
                   headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


# ── web/routers/media.py 的 URL 拼 token ──────────────────────────

def _fake_request(headers: dict):
    from types import SimpleNamespace
    return SimpleNamespace(headers=headers)


def test_signed_media_url_appends_token():
    req = _fake_request({"Authorization": "Bearer abc123"})
    assert media_router._signed_media_url(req, "/media/tts/x.wav") == \
        "/media/tts/x.wav?token=abc123"


def test_signed_media_url_without_auth_unchanged():
    req = _fake_request({})
    assert media_router._signed_media_url(req, "/media/tts/x.wav") == "/media/tts/x.wav"


def test_signed_media_url_non_bearer_unchanged():
    req = _fake_request({"Authorization": "Basic dXNlcjpwdw=="})
    assert media_router._signed_media_url(req, "/media/tts/x.wav") == "/media/tts/x.wav"


async def test_gallery_urls_include_token(tmp_path, monkeypatch):
    """gallery 返回的 url 必须携带当前请求的 token。"""
    img_dir = tmp_path / "image"
    img_dir.mkdir(parents=True)
    (img_dir / "a.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(media_router, "MEDIA_ROOT", tmp_path)
    req = _fake_request({"Authorization": "Bearer tok123"})
    data = await media_router.gallery(req, type="image", page=0, limit=24)
    assert data.data and data.data[0]["url"] == "/media/image/a.png?token=tok123"


async def test_gallery_urls_without_token_stay_plain(tmp_path, monkeypatch):
    """无 Authorization 头时 url 保持原样（静态目录会 401，fail-closed）。"""
    img_dir = tmp_path / "image"
    img_dir.mkdir(parents=True)
    (img_dir / "a.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(media_router, "MEDIA_ROOT", tmp_path)
    req = _fake_request({})
    data = await media_router.gallery(req, type="image", page=0, limit=24)
    assert data.data and data.data[0]["url"] == "/media/image/a.png"
