"""测试 core/message.py 的 AgentMessage"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from core.message import AgentMessage


class TestAgentMessage(unittest.TestCase):
    """测试 AgentMessage dataclass 序列化与便捷方法"""

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict -> from_dict 后字段一致"""
        original = AgentMessage(
            sender="klee",
            receiver="nahida",
            msg_type="request",
            content="帮我看看这个",
            context={"depth": 1, "topic": "炸弹"},
            timestamp=1234567890.0,
        )
        restored = AgentMessage.from_dict(original.to_dict())
        self.assertEqual(restored.sender, "klee")
        self.assertEqual(restored.receiver, "nahida")
        self.assertEqual(restored.msg_type, "request")
        self.assertEqual(restored.content, "帮我看看这个")
        self.assertEqual(restored.context, {"depth": 1, "topic": "炸弹"})
        self.assertEqual(restored.timestamp, 1234567890.0)

    def test_context_default_empty_dict(self):
        """未传 context 时默认为空 dict"""
        msg = AgentMessage(
            sender="a", receiver="b", msg_type="status", content="ok"
        )
        self.assertEqual(msg.context, {})
        self.assertIsInstance(msg.context, dict)

    def test_timestamp_default_current_time(self):
        """未传 timestamp 时默认为当前时间"""
        before = time.time()
        msg = AgentMessage(
            sender="a", receiver="b", msg_type="status", content="ok"
        )
        after = time.time()
        self.assertGreaterEqual(msg.timestamp, before)
        self.assertLessEqual(msg.timestamp, after)

    def test_context_default_not_shared(self):
        """默认 context 不会在实例间共享（default_factory 隔离）"""
        msg1 = AgentMessage(sender="a", receiver="b", msg_type="status", content="1")
        msg2 = AgentMessage(sender="a", receiver="b", msg_type="status", content="2")
        msg1.context["key"] = "value"
        self.assertNotIn("key", msg2.context)

    def test_msg_type_values(self):
        """各 msg_type 值均可正常构造"""
        for mtype in ("request", "response", "question", "status"):
            msg = AgentMessage(
                sender="a", receiver="b", msg_type=mtype, content="x"
            )
            self.assertEqual(msg.msg_type, mtype)

    def test_is_delegate_request_true(self):
        """msg_type == request 时 is_delegate_request 返回 True"""
        msg = AgentMessage(
            sender="klee", receiver="nahida", msg_type="request", content="help"
        )
        self.assertTrue(msg.is_delegate_request())

    def test_is_delegate_request_false(self):
        """非 request 类型时 is_delegate_request 返回 False"""
        for mtype in ("response", "question", "status"):
            msg = AgentMessage(
                sender="nahida", receiver="klee", msg_type=mtype, content="x"
            )
            self.assertFalse(msg.is_delegate_request())

    def test_from_dict_missing_context_uses_default(self):
        """from_dict 在缺少 context 字段时使用空 dict"""
        data = {
            "sender": "a",
            "receiver": "b",
            "msg_type": "status",
            "content": "ok",
        }
        msg = AgentMessage.from_dict(data)
        self.assertEqual(msg.context, {})

    def test_from_dict_missing_timestamp_uses_current(self):
        """from_dict 在缺少 timestamp 字段时使用当前时间"""
        before = time.time()
        data = {
            "sender": "a",
            "receiver": "b",
            "msg_type": "status",
            "content": "ok",
        }
        msg = AgentMessage.from_dict(data)
        after = time.time()
        self.assertGreaterEqual(msg.timestamp, before)
        self.assertLessEqual(msg.timestamp, after)

    def test_to_dict_returns_independent_context(self):
        """to_dict 返回的 context 修改不影响原实例"""
        msg = AgentMessage(
            sender="a", receiver="b", msg_type="status", content="ok",
            context={"k": "v"},
        )
        d = msg.to_dict()
        d["context"]["k"] = "modified"
        self.assertEqual(msg.context["k"], "v")


if __name__ == '__main__':
    unittest.main()
