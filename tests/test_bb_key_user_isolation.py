"""黑板缓存 key 用户隔离测试（TDD：RED → GREEN）。

验证 ``_bb_task_key`` 将 user_id 纳入 key 计算，避免不同用户对同一子代理
提交相同任务文本时命中彼此的缓存，造成隐私串用。
"""
import hashlib

from agent_core.sub_agent_manager import SubAgentManagerMixin


def _legacy_key(agent_name: str, task: str, suffix: str = "") -> str:
    """旧版（未隔离 user_id 前）的 key 计算逻辑，用于断言向后兼容。"""
    h = hashlib.md5(task.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    key = f"bb:delegate:{agent_name}:{h}"
    if suffix:
        key += f":{suffix}"
    return key


def test_different_user_ids_produce_different_keys():
    """不同 user_id + 相同 agent/task → key 必须不同。"""
    k1 = SubAgentManagerMixin._bb_task_key("xiaoke", "帮我写个排序函数", user_id="alice")
    k2 = SubAgentManagerMixin._bb_task_key("xiaoke", "帮我写个排序函数", user_id="bob")
    assert k1 != k2


def test_same_user_same_task_same_key():
    """相同 user_id + 相同 task → key 稳定一致（缓存仍可命中）。"""
    k1 = SubAgentManagerMixin._bb_task_key("xiaoke", "帮我写个排序函数", user_id="alice")
    k2 = SubAgentManagerMixin._bb_task_key("xiaoke", "帮我写个排序函数", user_id="alice")
    assert k1 == k2


def test_empty_user_id_keeps_legacy_key():
    """user_id 为空时退化为旧格式，保持向后兼容。"""
    legacy = _legacy_key("xiaoke", "帮我写个排序函数")
    assert SubAgentManagerMixin._bb_task_key("xiaoke", "帮我写个排序函数") == legacy
    assert SubAgentManagerMixin._bb_task_key("xiaoke", "帮我写个排序函数", user_id="") == legacy


def test_suffix_still_appended():
    """suffix（如 factual）仍追加在 key 末尾。"""
    key = SubAgentManagerMixin._bb_task_key("xiaoli", "查天气", suffix="factual", user_id="alice")
    assert key.endswith(":factual")
    key_no_suffix = SubAgentManagerMixin._bb_task_key("xiaoli", "查天气", user_id="alice")
    assert key == key_no_suffix + ":factual"
