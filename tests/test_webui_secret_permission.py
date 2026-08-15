from __future__ import annotations

import os
import stat

from web.routers import auth


def test_existing_secret_permissions_are_enforced(monkeypatch, tmp_path):
    """VULN-10: 已存在的 webui_secret 文件权限为 0644 时，读取后应被校正为 0600。"""
    monkeypatch.setattr(auth, "_SECRET", "")
    monkeypatch.delenv("WEBUI_SECRET", raising=False)

    secret_path = tmp_path / "webui_secret"
    secret_path.write_text("existing-secret-value", encoding="utf-8")
    secret_path.chmod(0o644)
    monkeypatch.setattr(auth, "_get_secret_path", lambda: secret_path)

    result = auth._load_or_create_secret()

    assert result == "existing-secret-value"
    assert stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600
