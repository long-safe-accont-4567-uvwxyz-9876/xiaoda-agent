"""测试 atomic_write.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
import os
import json
import tempfile
import shutil
from pathlib import Path
from utils.atomic_write import atomic_write, atomic_json_write


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


class TestOpenValidatedRegression(unittest.TestCase):
    """file_tools_v2._open_validated 标志位回归测试。

    缺陷根因 (修复前):
        os.open(resolved, O_RDONLY if "r" in mode else O_RDWR)
        所有非 "r" 模式统一用 O_RDWR，缺少 O_CREAT / O_TRUNC / O_APPEND，导致：
        1. 新建文件抛出 FileNotFoundError (无 O_CREAT)
        2. 覆盖写更短内容时旧尾部残留 → 静默数据损坏 (无 O_TRUNC)

    本测试类覆盖所有常见 mode 组合，防止再次引入类似标志位错误。
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_mode_w_creates_new_file(self):
        """mode='w' 必须能创建不存在的文件（修复前：FileNotFoundError）"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "brand_new.txt")
        self.assertFalse(os.path.exists(target))
        with _open_validated(target, mode="w") as f:
            f.write("created")
        self.assertTrue(os.path.exists(target))
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "created")

    def test_mode_w_truncates_existing_file(self):
        """mode='w' 覆盖写入时必须截断旧内容（修复前：静默数据损坏，旧尾部残留）"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "overwrite.txt")
        # 先写入长内容
        original = "THIS_IS_THE_ORIGINAL_LONG_CONTENT_42_CHARS"
        with open(target, "w", encoding="utf-8") as f:
            f.write(original)
        self.assertEqual(os.path.getsize(target), len(original.encode("utf-8")))
        # 用更短内容覆盖
        with _open_validated(target, mode="w") as f:
            f.write("SHORT")
        # 断言文件大小与内容都完全匹配新值 (无残留)
        self.assertEqual(os.path.getsize(target), 5)
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "SHORT")

    def test_mode_a_appends_not_truncates(self):
        """mode='a' 必须追加而非截断"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "append.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("FIRST")
        with _open_validated(target, mode="a") as f:
            f.write("_SECOND")
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "FIRST_SECOND")

    def test_mode_r_reads_existing(self):
        """mode='r' 回归：读取存在的文件应正常工作"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "readback.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("DATA_HERE")
        with _open_validated(target, mode="r") as f:
            self.assertEqual(f.read(), "DATA_HERE")

    def test_mode_r_missing_raises_file_not_found(self):
        """mode='r' 读取不存在的文件必须抛出 FileNotFoundError"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "does_not_exist.txt")
        with self.assertRaises(FileNotFoundError):
            with _open_validated(target, mode="r") as f:
                f.read()

    def test_mode_wb_binary_write_and_truncation(self):
        """mode='wb' 二进制写：创建+截断均正确"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "binary.bin")
        # 先写入 50 字节垃圾
        with open(target, "wb") as f:
            f.write(b"\xff" * 50)
        # 写 5 字节新内容 → 大小必须是 5
        with _open_validated(target, mode="wb", encoding=None) as f:
            f.write(b"\x00\x01\x02\x03\x04")
        self.assertEqual(os.path.getsize(target), 5)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"\x00\x01\x02\x03\x04")

    def test_mode_x_exclusive_create(self):
        """mode='x' 排它创建：第一次成功，第二次必须 FileExistsError"""
        from tools.file_tools_v2 import _open_validated
        target = os.path.join(self.tmp_dir, "exclusive.txt")
        with _open_validated(target, mode="x") as f:
            f.write("first")
        with self.assertRaises(FileExistsError):
            with _open_validated(target, mode="x") as f:
                f.write("again")
        # 原内容不应被改变
        with open(target, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "first")


if __name__ == '__main__':
    unittest.main()
