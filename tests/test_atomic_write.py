"""测试 atomic_write.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from utils.atomic_write import atomic_json_write, atomic_write


class TestAtomicWrite(unittest.TestCase):
    """测试原子文件写入"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_atomic_write_text(self):
        """原子写入文本文件"""
        target = os.path.join(self.tmp_dir, "test.txt")
        atomic_write(target, "Hello, World!")
        self.assertTrue(os.path.exists(target))
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Hello, World!")

    def test_atomic_json_write(self):
        """原子写入 JSON 文件"""
        target = os.path.join(self.tmp_dir, "test.json")
        data = {"key": "value", "number": 42, "nested": {"a": 1}}
        atomic_json_write(target, data)
        self.assertTrue(os.path.exists(target))
        with open(target, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded["key"], "value")
        self.assertEqual(loaded["number"], 42)

    def test_atomic_write_preserves_content(self):
        """写入后内容正确（含中文和特殊字符）"""
        target = os.path.join(self.tmp_dir, "content.txt")
        content = "中文内容测试 🎉\n第二行\n第三行"
        atomic_write(target, content)
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), content)

    @unittest.skipIf(sys.platform == "win32", "Windows requires admin for symlinks")
    def test_symlink_protection(self):
        """符号链接不被替换为常规文件"""
        # 创建真实文件
        real_file = os.path.join(self.tmp_dir, "real.txt")
        with open(real_file, "w") as f:
            f.write("原始内容")

        # 创建符号链接指向真实文件
        symlink = os.path.join(self.tmp_dir, "link.txt")
        os.symlink(real_file, symlink)

        # 通过符号链接写入
        atomic_write(symlink, "新内容")

        # 验证符号链接仍然存在且是符号链接
        self.assertTrue(os.path.islink(symlink))
        # 验证真实文件被更新
        with open(real_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "新内容")


if __name__ == '__main__':
    unittest.main()
