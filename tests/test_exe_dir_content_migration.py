"""exe 目录用户内容迁移的逐项合并语义（config_paths._merge_exe_dir_user_content）。

字段机 0.5.80：NSIS 安装器先把旧 dist 壁纸备份进 ~/.ai-agent/media/wallpapers，
随后旧 exe 目录 media 的整体跳过式迁移因目标非空被跳过——用户上传壁纸永久
滞留在旧目录。逐项合并必须保住"目标已有内容 + 旧目录独有文件"的场景。
"""
from pathlib import Path

import config_paths


def _setup(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """返回 (exe_base, media_dir)，全部目录指向 tmp_path 下的隔离位置。"""
    home = tmp_path / "home"
    media_dir = home / "media"
    for attr in ("STICKER_DIR", "XIAOLI_STICKER_DIR", "AGENT_STICKER_BASE",
                 "VOICE_REF_DIR", "MEMORY_STATE_DIR", "PLUGINS_CONFIG_DIR"):
        monkeypatch.setattr(config_paths, attr, home / attr.lower())
    monkeypatch.setattr(config_paths, "MEDIA_DIR", media_dir)
    exe = tmp_path / "install"
    return exe, media_dir


def test_user_wallpaper_merges_into_seeded_target(tmp_path: Path, monkeypatch):
    """目标已被安装器备份填充（非空）时，旧 exe 目录的用户壁纸仍须迁入。"""
    exe, media_dir = _setup(monkeypatch, tmp_path)
    # 旧 exe 目录：用户上传的自定义壁纸（老版本存 exe 目录 media/）
    old_media = exe / "media" / "wallpapers"
    old_media.mkdir(parents=True)
    (old_media / "xiaoda_1724xxx.jpg").write_bytes(b"user-wallpaper")
    # 目标目录：安装器已备份的默认壁纸（非空 → 旧逻辑会整体跳过）
    seeded = media_dir / "wallpapers"
    seeded.mkdir(parents=True)
    (seeded / "webui_background.jpg").write_bytes(b"seeded-default")

    config_paths._merge_exe_dir_user_content(exe)

    assert (seeded / "xiaoda_1724xxx.jpg").read_bytes() == b"user-wallpaper"
    assert (seeded / "webui_background.jpg").read_bytes() == b"seeded-default"


def test_old_static_wallpaper_dir_merges(tmp_path: Path, monkeypatch):
    """v0.5.5x 静态架构壁纸目录（exe/web/dist/assets/wallpapers）同样逐项合并。"""
    exe, media_dir = _setup(monkeypatch, tmp_path)
    old_static = exe / "web" / "dist" / "assets" / "wallpapers"
    old_static.mkdir(parents=True)
    (old_static / "webui_background.jpg").write_bytes(b"old-static")
    seeded = media_dir / "wallpapers"
    seeded.mkdir(parents=True)
    (seeded / "xiaoda.jpg").write_bytes(b"seeded")

    config_paths._merge_exe_dir_user_content(exe)

    assert (seeded / "webui_background.jpg").read_bytes() == b"old-static"
    assert (seeded / "xiaoda.jpg").read_bytes() == b"seeded"


def test_never_overwrites_existing_target_file(tmp_path: Path, monkeypatch):
    """同名文件目标已存在 → 保留目标（可能是用户新数据），不覆盖。"""
    exe, media_dir = _setup(monkeypatch, tmp_path)
    old_media = exe / "media" / "wallpapers"
    old_media.mkdir(parents=True)
    (old_media / "custom.jpg").write_bytes(b"old")
    seeded = media_dir / "wallpapers"
    seeded.mkdir(parents=True)
    (seeded / "custom.jpg").write_bytes(b"new-user-data")

    config_paths._merge_exe_dir_user_content(exe)

    assert (seeded / "custom.jpg").read_bytes() == b"new-user-data"


def test_missing_exe_dirs_are_noop(tmp_path: Path, monkeypatch):
    exe, media_dir = _setup(monkeypatch, tmp_path)
    config_paths._merge_exe_dir_user_content(tmp_path / "nonexistent-install")
    assert not media_dir.exists()
