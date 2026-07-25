"""测试并发安全 — SecretsBroker 多线程并发访问

覆盖场景：
- 多线程同时获取凭证，确保每个线程获得唯一的临时 token
- 并发 revoke 和 is_valid 操作的一致性
- 并发 rotate 操作的安全性
- 并发 get_credential 时清理过期 token 的安全性
"""
import sys
import threading
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from security.secrets_broker import SecretsBroker, TemporaryCredential


def test_concurrent_get_credential_unique_tokens():
    """多线程并发获取凭证，每个线程应获得唯一的临时 token"""
    broker = SecretsBroker({"TEST_API_KEY": "sk-secret"})
    tokens: set[str] = set()
    lock = threading.Lock()
    threads = []

    def worker():
        cred = broker.get_credential("TEST_API_KEY")
        with lock:
            tokens.add(cred.access_token)

    for _ in range(10):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert len(tokens) == 10, f"期望 10 个唯一 token，实际 {len(tokens)}"


def test_concurrent_revoke_is_valid_consistency():
    """并发 revoke 和 is_valid 操作应保持一致性"""
    broker = SecretsBroker({"TEST_KEY": "sk-value"})
    cred = broker.get_credential("TEST_KEY")
    results: list[bool] = []
    lock = threading.Lock()

    def revoke_worker():
        broker.revoke(cred)

    def check_worker():
        time.sleep(0.001)
        result = broker.is_valid(cred)
        with lock:
            results.append(result)

    t_revoke = threading.Thread(target=revoke_worker)
    t_check = threading.Thread(target=check_worker)

    t_revoke.start()
    t_check.start()

    t_revoke.join()
    t_check.join()

    assert not any(results), f"revoke 后 is_valid 应为 False，但结果为 {results}"


def test_concurrent_rotate():
    """并发 rotate 操作不应导致状态不一致"""
    broker = SecretsBroker({"ROTATE_KEY": "sk-rotate"})
    old_cred = broker.get_credential("ROTATE_KEY")
    exceptions: list[Exception] = []
    lock = threading.Lock()

    def rotate_worker():
        try:
            broker.rotate("ROTATE_KEY")
        except Exception as e:
            with lock:
                exceptions.append(e)

    threads = []
    for _ in range(5):
        t = threading.Thread(target=rotate_worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert len(exceptions) == 0, f"并发 rotate 不应抛出异常: {exceptions}"
    assert not broker.is_valid(old_cred), "rotate 后旧 token 应失效"


def test_concurrent_cleanup_expired():
    """并发 get_credential 时清理过期 token 应安全"""
    now = [0.0]
    broker = SecretsBroker(
        {"CLEANUP_KEY": "sk-cleanup"},
        ttl_seconds=0.001,
        clock=lambda: now[0],
    )

    def worker():
        cred = broker.get_credential("CLEANUP_KEY")
        time.sleep(0.005)
        now[0] += 0.01
        cred2 = broker.get_credential("CLEANUP_KEY")
        return cred, cred2

    threads = []
    results = []
    lock = threading.Lock()

    def wrapped_worker():
        try:
            c1, c2 = worker()
            with lock:
                results.append((c1, c2))
        except Exception as e:
            with lock:
                results.append(e)

    for _ in range(20):
        t = threading.Thread(target=wrapped_worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    for r in results:
        assert not isinstance(r, Exception), f"并发操作不应抛异常: {r}"


def test_concurrent_list_active():
    """并发 list_active 应返回一致的结果"""
    broker = SecretsBroker({"LIST_KEY": "sk-list"})
    results: list[list[str]] = []
    lock = threading.Lock()

    def get_credential_worker():
        broker.get_credential("LIST_KEY")

    def list_worker():
        time.sleep(0.001)
        active = broker.list_active()
        with lock:
            results.append(active)

    threads = []
    for _ in range(5):
        t = threading.Thread(target=get_credential_worker)
        threads.append(t)
        t.start()

    for _ in range(10):
        t = threading.Thread(target=list_worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    for r in results:
        assert isinstance(r, list), f"list_active 应返回列表: {r}"
        assert all(isinstance(n, str) for n in r), "列表元素应为字符串"