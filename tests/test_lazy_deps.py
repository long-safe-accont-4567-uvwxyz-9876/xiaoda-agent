"""懒加载依赖模块测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from utils.lazy_deps import ensure, is_available, _spec_is_safe, LAZY_DEPS


class TestLazyDeps(unittest.TestCase):

    def test_spec_is_safe_normal_package(self):
        """正常包名通过安全检查"""
        self.assertTrue(_spec_is_safe("paddleocr"))
        self.assertTrue(_spec_is_safe("httpx"))
        self.assertTrue(_spec_is_safe("Pillow"))

    def test_spec_is_safe_rejects_url(self):
        """URL 被拒绝"""
        self.assertFalse(_spec_is_safe("https://evil.com/pkg"))
        self.assertFalse(_spec_is_safe("git+https://github.com/x/pkg"))

    def test_spec_is_safe_rejects_path(self):
        """文件路径被拒绝"""
        self.assertFalse(_spec_is_safe("/etc/passwd"))
        self.assertFalse(_spec_is_safe("src/pkg"))

    def test_spec_is_safe_rejects_shell_chars(self):
        """Shell 元字符被拒绝"""
        self.assertFalse(_spec_is_safe("pkg;rm -rf /"))
        self.assertFalse(_spec_is_safe("pkg&&evil"))
        self.assertFalse(_spec_is_safe("pkg`evil`"))

    def test_spec_is_safe_rejects_empty(self):
        """空字符串被拒绝"""
        self.assertFalse(_spec_is_safe(""))
        self.assertFalse(_spec_is_safe("  "))

    def test_lazy_deps_whitelist_not_empty(self):
        """白名单不为空"""
        self.assertGreater(len(LAZY_DEPS), 0)

    def test_lazy_deps_whitelist_has_required_keys(self):
        """白名单条目包含必要字段"""
        for name, spec in LAZY_DEPS.items():
            self.assertIn("packages", spec)
            self.assertIn("description", spec)
            self.assertIsInstance(spec["packages"], list)

    def test_is_available_unknown_feature(self):
        """未知特性返回 False"""
        self.assertFalse(is_available("nonexistent_feature_xyz"))

    def test_ensure_unknown_feature(self):
        """未知特性返回 False"""
        self.assertFalse(ensure("nonexistent_feature_xyz"))


if __name__ == "__main__":
    unittest.main()
