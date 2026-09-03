from __future__ import annotations

import io
import sqlite3
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile

from web.routers import chat, local_ai, market, mcp, plugins, schedule, system, wechat
from web.upload_utils import read_upload_limited, validate_document_content, validate_image_content


def _request(**state):
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(**state)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        (market.uninstall_plugin, market.UninstallRequest(item_id="p")),
        (market.uninstall_skill, market.UninstallRequest(item_id="s")),
        (market.uninstall_mcp, market.UninstallRequest(item_id="m")),
    ],
)
async def test_market_uninstall_requires_confirmation_before_installer_lookup(
    monkeypatch, handler, payload,
):
    lookup = Mock()
    monkeypatch.setattr(market, "_get_installer", lookup)

    with pytest.raises(HTTPException) as error:
        await handler(payload, _request())

    assert error.value.status_code == 400
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_doctor_fix_requires_confirmation_before_creating_doctor(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("core.doctor._create_default_doctor", factory)

    with pytest.raises(HTTPException) as error:
        await system.run_doctor_fix(_request())

    assert error.value.status_code == 400
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_wechat_stop_requires_confirmation_before_clearing_credentials(monkeypatch):
    clear = Mock()
    monkeypatch.setattr(wechat, "clear_credentials", clear)

    with pytest.raises(HTTPException) as error:
        await wechat.stop_bot(_request(wechat_bot=None))

    assert error.value.status_code == 400
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_create_does_not_return_an_unrelated_row_after_insert_failure():
    database = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("write failed")),
        fetch_one=AsyncMock(return_value={"id": 99}),
    )
    request = _request(core=SimpleNamespace(db=database))

    with pytest.raises(RuntimeError, match="write failed"):
        await schedule.create_greeting(
            {"type": "fixed", "time": "09:00", "days": [1], "channels": ["web"]},
            request,
        )

    database.fetch_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_create_only_falls_back_for_missing_user_id_column(monkeypatch):
    database = SimpleNamespace(
        execute=AsyncMock(side_effect=[sqlite3.OperationalError("no such column: user_id"), 7]),
        fetch_one=AsyncMock(return_value={"id": 7}),
        insert_audit_log=AsyncMock(),
        commit=AsyncMock(),
    )
    request = _request(core=SimpleNamespace(db=database))

    result = await schedule.create_greeting(
        {"type": "fixed", "time": "09:00", "days": [1], "channels": ["web"]},
        request,
    )

    assert result.data == {"id": 7}
    assert database.execute.await_count == 2
    database.fetch_one.assert_awaited_once_with(
        "SELECT * FROM greeting_schedules WHERE id=?", (7,)
    )


@pytest.mark.asyncio
async def test_mcp_delete_keeps_client_and_config_when_stop_fails(monkeypatch):
    client = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("still running")))
    manager = SimpleNamespace(_clients={"srv": client})
    config = SimpleNamespace(
        get=lambda key: {"command": "npx"} if key == "mcp.srv" else None,
        delete=AsyncMock(),
    )
    monkeypatch.setattr(mcp, "_cfg", lambda: config)
    request = _request(core=SimpleNamespace(_mcp_manager=manager))
    request.headers = {"X-Confirm": "yes"}

    with pytest.raises(HTTPException) as error:
        await mcp.delete_server("srv", request)

    assert error.value.status_code == 500
    assert manager._clients["srv"] is client
    config.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_config_rejects_unknown_plugin_without_writing():
    manager = SimpleNamespace(
        get_plugin=lambda _plugin_id: None,
        set_plugin_config=AsyncMock(),
    )

    with pytest.raises(HTTPException) as error:
        await plugins.set_plugin_config(
            "missing", plugins.PluginConfigRequest(config={"x": 1}),
            _request(plugin_manager=manager),
        )

    assert error.value.status_code == 404
    manager.set_plugin_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_remove_keeps_registry_when_directory_delete_fails(tmp_path, monkeypatch):
    directory = tmp_path / "model"
    directory.mkdir()
    model = SimpleNamespace(id="model", removable=True, directory=str(directory))
    models = SimpleNamespace(
        get=AsyncMock(return_value=model),
        remove=AsyncMock(),
        register=AsyncMock(),
    )
    services = SimpleNamespace(
        models=models,
        instances=SimpleNamespace(model_in_use=lambda _model_id: False),
        downloads=SimpleNamespace(active_for_model=lambda _model_id: []),
        request_results={}, request_inputs={},
    )
    monkeypatch.setattr(local_ai.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError("busy")))
    request = _request(local_ai=services)
    request.headers = {"X-Confirm": "yes"}

    with pytest.raises(HTTPException) as error:
        await local_ai.remove_model("model", request)

    assert error.value.status_code == 500
    models.remove.assert_awaited_once_with("model")
    models.register.assert_awaited_once_with(model)
    assert directory.exists()
    assert not list(tmp_path.glob(".model.delete-*"))


@pytest.mark.asyncio
async def test_model_remove_restores_directory_when_registry_delete_fails(tmp_path):
    directory = tmp_path / "model"
    directory.mkdir()
    (directory / "weights.bin").write_bytes(b"weights")
    model = SimpleNamespace(id="model", removable=True, directory=str(directory))
    models = SimpleNamespace(
        get=AsyncMock(return_value=model),
        remove=AsyncMock(side_effect=RuntimeError("database unavailable")),
        register=AsyncMock(),
    )
    services = SimpleNamespace(
        models=models,
        instances=SimpleNamespace(model_in_use=lambda _model_id: False),
        downloads=SimpleNamespace(active_for_model=lambda _model_id: []),
        request_results={}, request_inputs={},
    )
    request = _request(local_ai=services)
    request.headers = {"X-Confirm": "yes"}

    with pytest.raises(RuntimeError, match="database unavailable"):
        await local_ai.remove_model("model", request)

    assert directory.is_dir()
    assert (directory / "weights.bin").read_bytes() == b"weights"
    assert not list(tmp_path.glob(".model.delete-*"))
    models.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_upload_limited_rejects_empty_content():
    upload = UploadFile(filename="empty.txt", file=io.BytesIO(b""))
    with pytest.raises(HTTPException, match="不能为空"):
        await read_upload_limited(upload, 1024, "文档")


def test_upload_content_validators_reject_spoofed_files():
    with pytest.raises(HTTPException, match="图片内容无效"):
        validate_image_content(b"not an image", ".png")
    with pytest.raises(HTTPException, match="PDF 文件内容无效"):
        validate_document_content(b"not a pdf", ".pdf")
    with pytest.raises(HTTPException, match="Office 文件内容无效"):
        validate_document_content(b"not a zip", ".docx")


def test_ooxml_validator_requires_expected_container_members():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", "<p:presentation/>")

    validate_document_content(payload.getvalue(), ".pptx")
    with pytest.raises(HTTPException, match="Office 文件结构无效"):
        validate_document_content(payload.getvalue(), ".docx")


def test_asr_client_is_closed_after_transcription(monkeypatch):
    client = SimpleNamespace(
        audio=SimpleNamespace(
            transcriptions=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(text="ok")),
            ),
        ),
        close=Mock(),
    )
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: client)

    assert chat._asr_via_openai("key", "https://example.com/v1", "asr", b"audio") == "ok"
    client.close.assert_called_once_with()
