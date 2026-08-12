import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from core.background_tasks import (
    BackgroundTaskManager,
    reset_current_request_context,
    set_current_request_context,
)
from db.database import DatabaseManager
from web.routers.chat import decode_history_context
from web.ws_hub import _handle_chat, build_chat_request_context, manager

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_attachment_only_request_builds_safe_snapshot(tmp_path, monkeypatch):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    image = upload_dir / "a.png"
    image.write_bytes(b"image")
    monkeypatch.setattr("web.ws_hub.MEDIA_ROOT", tmp_path)

    context = build_chat_request_context({
        "text": "",
        "image_url": "/media/upload/a.png",
        "image_name": "a.png",
        "search_mode": True,
    })

    assert context == {
        "text": "",
        "search": True,
        "think": False,
        "attachments": [{
            "kind": "image",
            "url": "/media/upload/a.png",
            "name": "a.png",
        }],
    }


def test_request_snapshot_discards_unsafe_attachment_paths(tmp_path, monkeypatch):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    monkeypatch.setattr("web.ws_hub.MEDIA_ROOT", tmp_path)

    context = build_chat_request_context({
        "text": "hello",
        "image_url": "/media/private/secret.png",
        "doc_path": "/etc/passwd",
    })

    assert context == {
        "text": "hello",
        "search": False,
        "think": False,
        "attachments": [],
    }


@pytest.mark.asyncio
async def test_migration_v26_adds_request_context_column():
    connection = await aiosqlite.connect(":memory:")
    try:
        await connection.execute("CREATE TABLE conversation_logs (id INTEGER PRIMARY KEY)")
        db = DatabaseManager.__new__(DatabaseManager)
        db._conn = connection
        await db._migrate_v26()
        columns = await connection.execute_fetchall("PRAGMA table_info(conversation_logs)")
        assert {row[1] for row in columns} >= {"request_context_json"}
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_migration_v26_is_idempotent_and_preserves_old_rows():
    connection = await aiosqlite.connect(":memory:")
    try:
        await connection.execute(
            "CREATE TABLE conversation_logs (id INTEGER PRIMARY KEY, user_message TEXT)"
        )
        await connection.execute(
            "INSERT INTO conversation_logs (id, user_message) VALUES (1, 'old')"
        )
        db = DatabaseManager.__new__(DatabaseManager)
        db._conn = connection
        await db._migrate_v26()
        await db._migrate_v26()
        row = await connection.execute_fetchall(
            "SELECT user_message, request_context_json FROM conversation_logs WHERE id=1"
        )
        assert row == [("old", "{}")]
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_insert_conversation_log_keeps_legacy_positional_auto_commit():
    db = DatabaseManager.__new__(DatabaseManager)
    db._conn = MagicMock()
    db._conn.execute = AsyncMock()
    db._conn.commit = AsyncMock()

    await db.insert_conversation_log("u", "web", "hello", "reply", "", "", "s", False)

    db._conn.commit.assert_not_awaited()
    assert db._conn.execute.await_args.args[1][-1] == "{}"


@pytest.mark.asyncio
async def test_background_persistence_serializes_current_request_context():
    db = MagicMock()
    db.insert_conversation_log = AsyncMock()
    db.update_session = AsyncMock()
    transaction = AsyncMock()
    transaction.__aenter__.return_value = None
    transaction.__aexit__.return_value = None
    db.write_transaction.return_value = transaction
    context = MagicMock(history=[])
    manager = BackgroundTaskManager(db=db, context=context, memory=None)
    request_context = {
        "text": "",
        "search": False,
        "think": False,
        "attachments": [{"kind": "image", "url": "/media/upload/a.png", "name": "a.png"}],
    }
    token = set_current_request_context(request_context)
    try:
        await manager._run_persistence_tasks(
            user_input="📷 图片",
            reply="done",
            user_id="u1",
            source="web",
            emotion={},
            session_id="s1",
        )
    finally:
        reset_current_request_context(token)

    kwargs = db.insert_conversation_log.await_args.kwargs
    assert json.loads(kwargs["request_context_json"]) == request_context


@pytest.mark.asyncio
async def test_background_request_context_is_isolated_between_concurrent_tasks():
    captured: list[tuple[str, dict]] = []

    async def persist(label: str, request_context: dict) -> None:
        db = MagicMock()
        db.insert_conversation_log = AsyncMock()
        db.update_session = AsyncMock()
        transaction = AsyncMock()
        transaction.__aenter__.return_value = None
        transaction.__aexit__.return_value = None
        db.write_transaction.return_value = transaction
        manager = BackgroundTaskManager(db=db, context=MagicMock(history=[]), memory=None)
        token = set_current_request_context(request_context)
        try:
            await asyncio.sleep(0)
            await manager._run_persistence_tasks(label, "done", label, "web", {}, label)
        finally:
            reset_current_request_context(token)
        raw = db.insert_conversation_log.await_args.kwargs["request_context_json"]
        captured.append((label, json.loads(raw)))

    contexts = {
        "first": {"text": "first", "search": False, "think": False, "attachments": []},
        "second": {"text": "second", "search": True, "think": False, "attachments": []},
    }
    await asyncio.gather(*(persist(label, context) for label, context in contexts.items()))

    assert dict(captured) == contexts


@pytest.mark.parametrize("raw", [None, "", "{broken", "[]", '"text"'])
def test_invalid_history_context_degrades_to_none(raw):
    assert decode_history_context(raw) is None


def test_valid_history_context_is_decoded():
    context = {"text": "hello", "search": False, "think": True, "attachments": []}
    assert decode_history_context(json.dumps(context)) == context


def test_valid_history_attachment_is_decoded(tmp_path, monkeypatch):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    document = upload_dir / "a.pdf"
    document.write_bytes(b"document")
    monkeypatch.setattr("web.routers.chat.UPLOAD_DIR", upload_dir)
    context = {
        "text": "hello",
        "search": False,
        "think": False,
        "attachments": [{
            "kind": "document",
            "url": "/media/upload/a.pdf",
            "name": "a.pdf",
            "path": str(document),
            "ext": ".pdf",
        }],
    }

    assert decode_history_context(json.dumps(context)) == context


@pytest.mark.parametrize(
    "context",
    [
        {"text": 1, "search": False, "think": False, "attachments": []},
        {"text": "hello", "search": "yes", "think": False, "attachments": []},
        {"text": "hello", "search": False, "think": False, "attachments": {}},
        {"text": "hello", "search": False, "think": False, "attachments": [], "secret": "x"},
        {"text": "hello", "search": False, "think": False, "attachments": [{"kind": "image", "url": "https://evil.test/a.png", "name": "a.png"}]},
        {"text": "hello", "search": False, "think": False, "attachments": [{"kind": "document", "url": "/media/upload/a.pdf", "name": "a.pdf", "path": "/etc/passwd"}]},
    ],
)
def test_history_context_rejects_invalid_snapshot_shapes(context):
    assert decode_history_context(json.dumps(context)) is None


def test_legacy_doc_marker_accepts_only_existing_upload(tmp_path, monkeypatch):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    document = upload_dir / "safe.pdf"
    document.write_bytes(b"document")
    monkeypatch.setattr("web.ws_hub.MEDIA_ROOT", tmp_path)

    context = build_chat_request_context({"text": f"summarize\n[Doc: {document}]"})

    assert context["text"] == "summarize"
    assert context["attachments"] == [{
        "kind": "document",
        "url": "/media/upload/safe.pdf",
        "name": "safe.pdf",
        "path": str(document),
        "ext": ".pdf",
    }]


def test_legacy_doc_marker_cannot_forward_arbitrary_path(tmp_path, monkeypatch):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    monkeypatch.setattr("web.ws_hub.MEDIA_ROOT", tmp_path)

    context = build_chat_request_context({"text": "read this\n[Doc: /etc/passwd]"})

    assert context["text"] == "read this"
    assert context["attachments"] == []


@pytest.mark.asyncio
async def test_legacy_doc_marker_reaches_processor_only_after_validation(tmp_path, monkeypatch):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    document = upload_dir / "safe.pdf"
    document.write_bytes(b"document")
    monkeypatch.setattr("web.ws_hub.MEDIA_ROOT", tmp_path)
    process = AsyncMock(return_value={"reply": "done"})
    send = AsyncMock()
    monkeypatch.setattr("web.ws_hub.process_and_serialize", process)
    monkeypatch.setattr(manager, "send_to", send)
    app = MagicMock()
    app.state.core = MagicMock()
    ws = MagicMock(scope={"app": app})

    try:
        await _handle_chat(
            "legacy-doc", {"text": f"summarize\n[Doc: {document}]"}, "m1", ws,
        )
    finally:
        manager._session_map.pop("legacy-doc", None)

    process.assert_awaited_once()
    assert process.await_args.args[1] == "summarize"
    assert str(document) in process.await_args.kwargs["system_context"]


def test_history_and_frontend_restore_request_snapshots():
    router = source("web/routers/chat.py")
    schemas = source("web/schemas.py")
    chat = source("web/frontend/src/stores/chat.ts")
    assert "request_context_json" in router
    assert "request_context: dict | None = None" in schemas
    assert "request: h.request_context || undefined" in chat
    assert "structuredClone(message.request)" in chat
