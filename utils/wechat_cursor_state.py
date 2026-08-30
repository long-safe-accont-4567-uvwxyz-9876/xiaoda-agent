"""微信游标与死信状态文件的原子读写。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from utils.atomic_write import atomic_write

WECHAT_CURSOR_STATE_LOCK = threading.Lock()


def load_cursor_state(path: Path, ttl: float) -> tuple[str, dict[str, float]]:
    """读取游标并丢弃过期或损坏的死信条目。"""
    with WECHAT_CURSOR_STATE_LOCK:
        data = json.loads(path.read_text(encoding="utf-8"))
    now = time.time()
    dead: dict[str, float] = {}
    for msg_id, value in (data.get("dead") or {}).items():
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            continue
        if now - timestamp <= ttl:
            dead[str(msg_id)] = timestamp
    return str(data.get("cursor", "") or ""), dead


def _token_matches(credentials_path: Path, expected_token: str, *, allow_missing: bool) -> bool:
    if not credentials_path.exists():
        return allow_missing
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(credentials, dict) and credentials.get("bot_token", "") == expected_token


def write_cursor_state(
    path: Path,
    credentials_path: Path,
    expected_token: str,
    cursor: str,
    dead: dict[str, float],
) -> bool:
    """锁内校验凭证归属，再以 0600 原子替换状态文件。"""
    if not expected_token or (not cursor and not dead):
        return False
    content = json.dumps({"cursor": cursor, "dead": dead}, ensure_ascii=False)
    with WECHAT_CURSOR_STATE_LOCK:
        if not _token_matches(credentials_path, expected_token, allow_missing=False):
            return False
        atomic_write(path, content, mode=0o600, encoding="utf-8")
    return True


def update_probe_cursor(
    path: Path,
    credentials_path: Path,
    expected_token: str,
    cursor: str,
) -> bool:
    """更新探针游标，同时保留同文件中的死信表。"""
    if not cursor or not expected_token:
        return False
    with WECHAT_CURSOR_STATE_LOCK:
        if not _token_matches(credentials_path, expected_token, allow_missing=True):
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        content = json.dumps({"cursor": cursor, "dead": data.get("dead") or {}}, ensure_ascii=False)
        atomic_write(path, content, mode=0o600, encoding="utf-8")
    return True
