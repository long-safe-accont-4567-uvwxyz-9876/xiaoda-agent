"""public-wallpaper 死链闸门（web.routers.agents._existing_wallpaper_url）。

字段机 0.5.80：老配置残留的壁纸 URL 指向已丢失文件，接口原样返回死链，
登录页 CSS 背景 404 静默空白且默认壁纸永不被尝试。
"""
from pathlib import Path

import config
from web.routers.agents import _existing_wallpaper_url


def test_missing_wallpaper_file_returns_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    assert _existing_wallpaper_url("/media/wallpapers/xiaoda.jpg") == ""


def test_existing_wallpaper_file_passes_through(tmp_path: Path, monkeypatch):
    wallpapers = tmp_path / "wallpapers"
    wallpapers.mkdir()
    (wallpapers / "xiaoda.jpg").write_bytes(b"fake")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    assert _existing_wallpaper_url("/media/wallpapers/xiaoda.jpg") == "/media/wallpapers/xiaoda.jpg"


def test_non_media_path_passes_through(tmp_path: Path, monkeypatch):
    """非 /media/wallpapers/ 路径（外链等）不做本地存在性判断。"""
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    url = "https://example.com/bg.jpg"
    assert _existing_wallpaper_url(url) == url


def test_traversal_attempts_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    assert _existing_wallpaper_url("/media/wallpapers/../secrets.json") == ""
    assert _existing_wallpaper_url("/media/wallpapers/a/b.jpg") == ""
    assert _existing_wallpaper_url("/media/wallpapers/") == ""
