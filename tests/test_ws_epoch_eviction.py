"""Task 3: 凭证轮换驱逐既有 WebSocket 连接测试。

背景：改密/恢复/revoke-all 递增 token epoch，但 /ws 仅握手时验 token，
旧 epoch 连接在 REST 已 401 后仍可继续 chat/terminal 操作。修复后：

- ConnectionManager.register 记录连接认证时的 token epoch（可选参数，向后兼容）；
- close_all_for_epoch(epoch) 关闭所有 epoch 严格小于给定值的连接（4001 关闭，
  对齐未授权语义），映射清理复用 unregister；
- auth 三端点（recover / change_password / revoke_all）在 bump 后调用驱逐。

连接一律通过 register() 构造（心跳/写入任务由 register 真实启动，
参照 tests/test_ws_heartbeat.py 的做法，不手工注入内部映射）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import security.recovery_qa as rqa
from web.routers import auth
from web.ws_hub import ConnectionManager, _extract_token_epoch, manager, websocket_endpoint

# ── 公共隔离工具（参照 test_auth_recover.py / test_auth_change_password.py）──


def _isolate_auth(tmp_path, monkeypatch) -> None:
    """auth/rqa 落盘路径全部重定向到 tmp_path，绝不触碰真实 .env / credentials/。"""
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_revoked_path", lambda: tmp_path / "revoked.json")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    monkeypatch.setattr(rqa, "_get_path", lambda: tmp_path / "webui_recovery.json")
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    auth._tokens.clear()
    auth._revoked_cache.clear()
    auth._revoked_cache_mtime = 0.0
    auth._rate_limit.clear()
    if getattr(auth, "_revoked_grace", None) is not None:
        auth._revoked_grace.clear()


def _patch_eviction(monkeypatch) -> AsyncMock:
    """把单例 manager 的驱逐方法替换为 AsyncMock（端点内延迟导入拿到的是该单例）。"""
    evict = AsyncMock(return_value=0)
    monkeypatch.setattr(manager, "close_all_for_epoch", evict)
    return evict


def _fake_request() -> SimpleNamespace:
    """recover 端点直调所需的极简 request（client/headers/app.state.core 缺失路径）。"""
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace()),
    )


def _mock_ws(subprotocol=None):
    """构造带 headers 的 mock WebSocket（参照 test_ws_unauthorized_no_reconnect.py）。"""
    ws = AsyncMock()
    ws.headers = SimpleNamespace(get=lambda key: subprotocol)
    return ws


# ── close_all_for_epoch：连接管理器行为 ──────────────────────────


async def test_close_all_for_epoch_evicts_strictly_older_connections():
    """epoch N 注册的两个连接在 close_all_for_epoch(N+1) 后：4001 关闭 + 映射清理。"""
    mgr = ConnectionManager()
    ws_old_a, ws_old_b, ws_new = AsyncMock(), AsyncMock(), AsyncMock()
    cid_a = mgr.register(ws_old_a, token_epoch=5)
    cid_b = mgr.register(ws_old_b, token_epoch=5)
    cid_new = mgr.register(ws_new, token_epoch=6)

    evicted = await mgr.close_all_for_epoch(6)

    assert evicted == 2
    # 4001 关闭码 + 未授权 reason（前端 onclose 对 4001 停止重连的契约）
    ws_old_a.close.assert_any_await(code=4001, reason="Unauthorized")
    ws_old_b.close.assert_any_await(code=4001, reason="Unauthorized")
    # 所有映射（含 epoch 登记、心跳/写入任务、发送队列）已清理
    for mapping in (mgr._connections, mgr._conn_epochs, mgr._agent_map,
                    mgr._session_map, mgr._heartbeat_tasks, mgr._pong_events,
                    mgr._send_queues, mgr._writer_tasks):
        assert cid_a not in mapping, f"{cid_a} 残留于 {mapping}"
        assert cid_b not in mapping, f"{cid_b} 残留于 {mapping}"
    # 以新 epoch 注册的连接不受影响
    assert cid_new in mgr._connections
    assert mgr._conn_epochs[cid_new] == 6
    ws_new.close.assert_not_awaited()


async def test_close_all_for_epoch_noop_when_nothing_strictly_older():
    """未记录 epoch 与同 epoch 连接均不驱逐（严格小于语义 + 向后兼容）。"""
    mgr = ConnectionManager()
    ws_none, ws_cur = AsyncMock(), AsyncMock()
    cid_none = mgr.register(ws_none)  # 未提供 epoch（旧调用方形态）
    cid_cur = mgr.register(ws_cur, token_epoch=7)

    assert await mgr.close_all_for_epoch(7) == 0

    ws_none.close.assert_not_awaited()
    ws_cur.close.assert_not_awaited()
    assert cid_none in mgr._connections and cid_cur in mgr._connections
    await mgr.unregister(cid_none)
    await mgr.unregister(cid_cur)
    # 空管理器上的驱逐调用为安全空操作
    assert await ConnectionManager().close_all_for_epoch(1) == 0


# ── 握手：epoch 提取与登记 ────────────────────────────────────────


def test_extract_token_epoch_roundtrip_with_real_token(tmp_path, monkeypatch):
    """_extract_token_epoch 对 auth._issue_token 真实 token 格式的往返解析。"""
    _isolate_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "_token_epoch", 7)
    token, _ = auth._issue_token()

    assert _extract_token_epoch(token) == 7
    # 非法输入一律返回 None（握手侧按"未记录"处理，不影响连接建立）
    assert _extract_token_epoch("not-a-token") is None
    assert _extract_token_epoch("") is None


async def test_handshake_passes_token_epoch_to_register(tmp_path, monkeypatch):
    """握手端点把 token 内嵌的 epoch 作为 token_epoch 传给 register。"""
    _isolate_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "_token_epoch", 7)
    token, _ = auth._issue_token()

    captured: dict = {}

    def _fake_register(_ws, token_epoch=None):
        captured["epoch"] = token_epoch
        raise ValueError("连接数已达上限")  # 借用既有分支提前结束端点

    ws = _mock_ws(subprotocol=token)
    with patch("web.routers.auth._validate_token", return_value=True), \
            patch("web.ws_hub.manager.register", side_effect=_fake_register):
        await websocket_endpoint(ws)

    assert captured["epoch"] == 7


# ── 三端点：bump 后必须调用驱逐 ──────────────────────────────────


async def test_recover_evicts_old_epoch_connections(tmp_path, monkeypatch):
    """POST /auth/recover 成功后：以新 epoch 调用驱逐。"""
    _isolate_auth(tmp_path, monkeypatch)
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    # .env 写入替换为空操作（recover 会经 asyncio.to_thread 调用它）
    monkeypatch.setattr(auth, "_update_env_password", lambda _pwd: None)
    evict = _patch_eviction(monkeypatch)

    await auth.recover(
        auth.RecoverRequest(answer="miaomiao", new_password="newpass123"),
        request=_fake_request(), response=None)

    evict.assert_awaited_once_with(auth._load_token_epoch())
    assert evict.await_args.args[0] >= 1  # bump 后的新 epoch


async def test_change_password_evicts_old_epoch_connections(tmp_path, monkeypatch):
    """POST /auth/change-password 成功后：以新 epoch 调用驱逐。"""
    _isolate_auth(tmp_path, monkeypatch)
    monkeypatch.setenv("WEBUI_PASSWORD", "oldpass123")
    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    monkeypatch.setattr(auth, "_update_env_password", lambda _pwd: None)
    evict = _patch_eviction(monkeypatch)

    await auth.change_password(
        auth.ChangePasswordRequest(
            old_password="oldpass123", new_password="newpass123",
            answer="miaomiao"),
        user_id="webui", request=None, response=None)

    evict.assert_awaited_once_with(auth._load_token_epoch())
    assert evict.await_args.args[0] >= 1


async def test_revoke_all_evicts_old_epoch_connections(tmp_path, monkeypatch):
    """POST /auth/revoke-all：以新 epoch 调用驱逐。"""
    _isolate_auth(tmp_path, monkeypatch)
    auth._issue_token()  # 留一个旧 token 验证端点其余语义不变
    evict = _patch_eviction(monkeypatch)

    await auth.revoke_all(user_id="webui")

    evict.assert_awaited_once_with(auth._load_token_epoch())
    assert evict.await_args.args[0] >= 1
    # 既有语义保持：旧 token 全部吊销
    assert not auth._tokens
