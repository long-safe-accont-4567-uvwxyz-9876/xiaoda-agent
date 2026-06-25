"""测试 agent_core/shared_blackboard.py — 协程安全 KV、TTL、订阅、并发、清理。

风格参考 tests/test_instinct_manager.py：unittest + asyncio.run + mock。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
import asyncio

from agent_core.shared_blackboard import SharedBlackboard


class TestSharedBlackboard(unittest.TestCase):
    """SharedBlackboard：put/get/TTL/subscribe/并发/清理"""

    def test_put_get(self):
        """put 后 get 返回对应值"""
        async def _run():
            bb = SharedBlackboard()
            await bb.put("k", "v")
            self.assertEqual(await bb.get("k"), "v")

        asyncio.run(_run())

    def test_get_missing(self):
        """get("missing") 返回 None"""
        async def _run():
            bb = SharedBlackboard()
            self.assertIsNone(await bb.get("missing"))

        asyncio.run(_run())

    def test_ttl_expiry(self):
        """ttl=0.1，sleep 0.2 后 get 返回 None"""
        async def _run():
            bb = SharedBlackboard()
            await bb.put("k", "v", ttl=0.1)
            self.assertEqual(await bb.get("k"), "v")  # 立即可读
            await asyncio.sleep(0.2)
            self.assertIsNone(await bb.get("k"))  # 过期

        asyncio.run(_run())

    def test_subscribe(self):
        """subscribe("k") 返回 Event，put 后 Event 被 set"""
        async def _run():
            bb = SharedBlackboard()
            ev = await bb.subscribe("k")
            self.assertIsInstance(ev, asyncio.Event)
            self.assertFalse(ev.is_set())
            await bb.put("k", "value")
            self.assertTrue(ev.is_set())
            self.assertEqual(await bb.get("k"), "value")

        asyncio.run(_run())

    def test_concurrent_access(self):
        """并发 put/get 不出错（asyncio.gather）"""
        async def _run():
            bb = SharedBlackboard()

            async def writer(i):
                await bb.put(f"k{i}", i, agent_name=f"agent{i}")

            async def reader(i):
                # 不存在的 key 读 None 不报错；存在的 key 读到值
                return await bb.get(f"k{i}")

            # 先写后并发读写
            await asyncio.gather(*(writer(i) for i in range(20)))
            results = await asyncio.gather(*(reader(i) for i in range(20)))
            self.assertEqual(sorted(results), list(range(20)))

            # 并发混合 put/get（同一组 key）不抛异常
            mixed = []
            for i in range(20):
                mixed.append(writer(i + 100))
                mixed.append(reader(i))  # 已写入的 key
            await asyncio.gather(*mixed, return_exceptions=False)

        asyncio.run(_run())

    def test_cleanup_expired(self):
        """过期条目被清理，cleanup_expired 返回清理数量"""
        async def _run():
            bb = SharedBlackboard()
            await bb.put("a", "1", ttl=0.1)
            await bb.put("b", "2", ttl=0.1)
            await bb.put("c", "3", ttl=10)  # 未过期
            await asyncio.sleep(0.2)
            count = await bb.cleanup_expired()
            self.assertEqual(count, 2)
            # 未过期的仍在
            self.assertEqual(await bb.get("c"), "3")
            self.assertIsNone(await bb.get("a"))

        asyncio.run(_run())


if __name__ == '__main__':
    unittest.main()
