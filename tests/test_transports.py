"""Transport 抽象层测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest

from transports import AgnesTransport, MiMoTransport
from transports.base import ProviderTransport, TransportResponse


class TestTransportResponse(unittest.TestCase):

    def test_default_values(self):
        """默认值为空"""
        resp = TransportResponse()
        self.assertEqual(resp.content, "")
        self.assertIsNone(resp.tool_calls)
        self.assertIsNone(resp.reasoning_content)
        self.assertIsNone(resp.usage)
        self.assertIsNone(resp.raw_response)

    def test_custom_values(self):
        """自定义值正确设置"""
        resp = TransportResponse(
            content="hello",
            tool_calls=[{"id": "1"}],
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )
        self.assertEqual(resp.content, "hello")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.usage["prompt_tokens"], 10)


class TestMiMoTransport(unittest.TestCase):

    def test_provider_name(self):
        """提供商名称为 mimo"""
        transport = MiMoTransport()
        self.assertEqual(transport.provider_name, "mimo")

    def test_is_available_with_key(self):
        """有 API Key 时可用"""
        import os
        key = os.getenv("MIMO_API_KEY", "")
        transport = MiMoTransport()
        if key:
            self.assertTrue(transport.is_available())
        else:
            self.assertFalse(transport.is_available())


class TestAgnesTransport(unittest.TestCase):

    def test_provider_name(self):
        """提供商名称为 agnes"""
        transport = AgnesTransport()
        self.assertEqual(transport.provider_name, "agnes")

    def test_is_available_with_key(self):
        """有 API Key 时可用，无 Key 时不可用"""
        import os
        key = os.getenv("AGNES_API_KEY", "")
        transport = AgnesTransport()
        if key:
            self.assertTrue(transport.is_available())
        else:
            # 无 Agnes API Key 时不可用
            self.assertFalse(transport.is_available())


class TestTransportBase(unittest.TestCase):

    def test_cannot_instantiate_base(self):
        """基类不能直接实例化"""
        with self.assertRaises(TypeError):
            ProviderTransport()


if __name__ == "__main__":
    unittest.main()
