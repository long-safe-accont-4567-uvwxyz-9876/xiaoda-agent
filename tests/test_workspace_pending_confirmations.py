"""审查 G2：命令确认请求在刷新后能被找回。

缺陷形态：确认请求只经 WS 推一次，谁刷新页面谁就永久丢失卡片，而后端仍挂着
这次确认直到超时；同时记录里只有 session_id/decision/_ts，连"在确认什么命令"
都没存，恢复出来也只能是空卡片。
"""
import asyncio
import time

from web.routers import workspace as ws


def _run(session_id=''):
    return asyncio.run(ws.list_pending_confirmations(session_id))


def _clean():
    ws._pending_cmd_decisions.clear()


def test_pending_confirmations_returns_command_and_deadline():
    _clean()
    ws.register_cmd_decision_scope('rq-1', 'sess-a', command='rm -rf /tmp/x')
    data = _run('sess-a')
    assert [r['request_id'] for r in data['requests']] == ['rq-1']
    assert data['requests'][0]['command'] == 'rm -rf /tmp/x'
    assert data['requests'][0]['session_id'] == 'sess-a'
    assert data['requests'][0]['expires_in'] > 0
    _clean()


def test_pending_confirmations_skips_decided_and_other_sessions():
    _clean()
    ws.register_cmd_decision_scope('rq-2', 'sess-a', command='ls')
    ws.register_cmd_decision_scope('rq-3', 'sess-b', command='whoami')
    assert [r['request_id'] for r in _run('sess-a')['requests']] == ['rq-2']
    ws._pending_cmd_decisions['rq-2']['decision'] = 'allow'
    assert _run('sess-a')['requests'] == []
    _clean()


def test_pending_confirmations_drops_expired():
    _clean()
    ws.register_cmd_decision_scope('rq-4', 'sess-a', command='sleep 1', deadline=time.time() - 1)
    assert _run('sess-a')['requests'] == []
    assert 'rq-4' not in ws._pending_cmd_decisions
    _clean()


def test_without_command_is_not_recoverable():
    """没记命令的旧记录不返回——否则前端补出来一张不知道在确认什么的卡片。"""
    _clean()
    ws.register_cmd_decision_scope('rq-5', 'sess-a')
    assert _run('sess-a')['requests'] == []
    _clean()
