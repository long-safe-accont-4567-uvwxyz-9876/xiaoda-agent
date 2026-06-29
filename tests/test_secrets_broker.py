"""测试 security/secrets_broker.py — Secrets Broker 凭证代理

覆盖场景：
- get_credential 返回临时 token 而非原始 key
- TTL 过期后临时凭证失效
- revoke 撤销后不可用
- rotate 轮换后旧 token 失效
- list_active 仅返回凭证名，不返回凭证值
- 操作被审计日志记录
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from security.secrets_broker import SecretsBroker, TemporaryCredential


def test_get_credential_returns_temporary():
    """返回临时 token，而非原始 API Key"""
    raw_key = "sk-original-secret-12345"
    broker = SecretsBroker({"OPENAI_API_KEY": raw_key})

    cred = broker.get_credential("OPENAI_API_KEY", caller="llm")

    assert isinstance(cred, TemporaryCredential)
    # 临时 token 不等于原始 key
    assert cred.access_token != raw_key
    assert raw_key not in cred.access_token
    # 临时 token 非空
    assert cred.access_token
    assert cred.scope == "default"
    # 当前有效
    assert broker.is_valid(cred)


def test_credential_expires():
    """TTL 过期后临时凭证失效（使用可注入时钟，避免真实 sleep）"""
    now = [1000.0]
    broker = SecretsBroker(
        {"X_API_KEY": "sk-secret"},
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    cred = broker.get_credential("X_API_KEY")

    # 签发时有效
    assert broker.is_valid(cred)
    # 时间推进超过 TTL
    now[0] += 11
    assert not broker.is_valid(cred)


def test_revoke():
    """撤销后临时凭证不可用"""
    broker = SecretsBroker({"SVC_TOKEN": "sk-abc"})
    cred = broker.get_credential("SVC_TOKEN")

    assert broker.is_valid(cred)
    assert broker.revoke(cred) is True
    assert not broker.is_valid(cred)


def test_rotate():
    """轮换后旧 token 失效，仍可签发新 token"""
    broker = SecretsBroker({"GITHUB_TOKEN": "ghp_xxx"})
    cred = broker.get_credential("GITHUB_TOKEN")

    assert broker.is_valid(cred)
    broker.rotate("GITHUB_TOKEN")
    # 轮换后旧 token 立即失效
    assert not broker.is_valid(cred)
    # 仍可签发新 token，且与旧 token 不同
    cred2 = broker.get_credential("GITHUB_TOKEN")
    assert broker.is_valid(cred2)
    assert cred2.access_token != cred.access_token


def test_list_active_no_values():
    """list_active 仅返回凭证名，不返回凭证值"""
    raw_key = "sk-super-secret-value-9999"
    broker = SecretsBroker({"OPENAI_API_KEY": raw_key})
    broker.get_credential("OPENAI_API_KEY")

    active = broker.list_active()

    # 凭证名出现
    assert "OPENAI_API_KEY" in active
    # 仅返回字符串名称
    assert all(isinstance(n, str) for n in active)
    # 不包含原始 key 值
    for n in active:
        assert raw_key not in n


def test_audit_log():
    """get / rotate / revoke 操作均被审计记录"""
    broker = SecretsBroker({"MIMO_API_KEY": "sk-audit"})

    broker.get_credential("MIMO_API_KEY", caller="alice")
    broker.rotate("MIMO_API_KEY", caller="bob")
    cred = broker.get_credential("MIMO_API_KEY", caller="alice")
    broker.revoke(cred, caller="carol")

    logs = broker.get_audit_log()
    # get + rotate + get + revoke = 4 条
    assert len(logs) == 4
    # 每条都包含 who / what / when
    for log in logs:
        assert log.who
        assert log.what in ("get", "rotate", "revoke")
        assert log.when > 0

    # 验证操作顺序与调用方
    ops = [(log.who, log.what) for log in logs]
    assert ops[0] == ("alice", "get")
    assert ops[1] == ("bob", "rotate")
    assert ops[2] == ("alice", "get")
    assert ops[3] == ("carol", "revoke")


def test_get_credential_unknown_raises():
    """未注册的凭证名应抛出 PermissionError（不泄漏存在性细节）"""
    broker = SecretsBroker({"A_API_KEY": "sk-a"})
    with pytest.raises(PermissionError):
        broker.get_credential("DOES_NOT_EXIST")
