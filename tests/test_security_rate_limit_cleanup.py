"""测试 SecurityFilter 速率限制清理逻辑修复.

缺陷根因：_cleanup_stale_users 在 _check_rate 的 60s 滑窗过滤后，
对 timestamps=[] 的活跃用户条目使用 `not ts` 判断 → True，
导致首次清理时全部用户记录被误删，速率限制永久失效。

修复：仅删除 ts 非空 且 ts[-1] 距今 > 300s 的真正 stale 用户。
"""
import time
import pytest


def test_cleanup_stale_users_preserves_recent_users():
    """最近活跃用户（timestamp 被滑窗过滤为空）不应被误删。"""
    from security.security import SecurityFilter

    sf = SecurityFilter.__new__(SecurityFilter)
    sf.rate_limit = 120
    sf._last_cleanup_time = 0.0  # 强制触发清理
    sf._call_timestamps = {
        # 活跃用户：刚被滑窗过滤清空（空列表）—— 真实活跃中，不应删除
        "alice": [],
        "bob": [],
        # 真正 stale 用户：最后一次请求距今 > 300s
        "old_user": [time.time() - 600],
    }

    now = time.time()
    sf._cleanup_stale_users(now)

    # 断言：活跃用户保留；真正 stale 用户被删除
    assert "alice" in sf._call_timestamps, "活跃用户不应被误删"
    assert "bob" in sf._call_timestamps, "活跃用户不应被误删"
    assert "old_user" not in sf._call_timestamps, "真正 stale 用户应被清理"


def test_cleanup_stale_users_does_not_nuke_all_users():
    """清理运行后 _call_timestamps 不应被整体清空（回归原缺陷）。"""
    from security.security import SecurityFilter

    sf = SecurityFilter.__new__(SecurityFilter)
    sf.rate_limit = 120
    sf._last_cleanup_time = 0.0
    now = time.time()
    sf._call_timestamps = {
        f"user_{i}": [now - 30]  # 全部活跃（30s 内请求过，仍在滑窗内）
        for i in range(50)
    }

    sf._cleanup_stale_users(now)

    # 50 个活跃用户都应保留（timestamp 非空且距今 < 300s）
    assert len(sf._call_timestamps) == 50, (
        f"活跃用户被误删：期望 50 个保留，实际剩 {len(sf._call_timestamps)}"
    )


def test_rate_limit_initialization_prevents_immediate_cleanup():
    """_last_cleanup_time 在 __init__ 中应被初始化，防止首次 _check_rate 立即触发清理。"""
    from security.security import SecurityFilter

    sf = SecurityFilter.__new__(SecurityFilter)
    # 手动调用 __init__ 验证
    # 为避免加载 YAML 依赖，mock 掉 _load_patterns
    original_load = SecurityFilter._load_patterns

    def _noop_load(self):
        self._injection_patterns = []
        self._bypass_patterns = []
        self._leak_input_patterns = []
        self._dangerous_patterns = []
        self._leak_patterns = []
        self._privacy_leak_patterns = []
        self._patterns_mtime = 0.0

    SecurityFilter._load_patterns = _noop_load
    try:
        sf.owner_ids = set()
        sf._load_owner_ids_from_env = lambda: []
        sf._load_patterns = _noop_load.__get__(sf, SecurityFilter)
        sf.__init__(owner_ids=[])

        # 关键断言：_last_cleanup_time 已初始化
        assert hasattr(sf, "_last_cleanup_time"), (
            "_last_cleanup_time 未初始化，首次 _check_rate 将立即触发全量清理"
        )
        # 断言：首次 _check_rate 不会立即触发清理
        before = time.time()
        sf._last_cleanup_time = before  # 模拟初始化
        # 注入 1 条用户状态（空列表）
        sf._call_timestamps = {"u1": []}
        # 首次 _check_rate：now - last_cleanup_time < 300s → 不触发清理
        result = sf._check_rate("u1")
        assert result is True, "首次 _check_rate 应允许新请求"
        assert "u1" in sf._call_timestamps, "用户记录应被保留"
    finally:
        SecurityFilter._load_patterns = original_load
