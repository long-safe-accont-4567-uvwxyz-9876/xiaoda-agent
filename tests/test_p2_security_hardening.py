"""P2 安全加固测试

覆盖:
1. market/installer: _security_scan 检测到高危模式时阻断安装 (不再只 log)
"""
import pytest
import asyncio


# ── market/installer: _security_scan 阻断 ───────────────────────

class TestSecurityScanBlock:
    """验证安全扫描检测到高危模式时阻断安装。"""

    def _make_installer(self):
        from market.installer import MarketInstaller
        from pathlib import Path
        import tempfile
        # _security_scan 不依赖 plugins_dir/plugin_manager，用临时目录即可
        tmp = Path(tempfile.mkdtemp())
        return MarketInstaller(plugins_dir=tmp / "plugins", skills_dir=tmp / "skills")

    def test_eval_blocked(self):
        """包含 eval() 的插件应被阻断。"""
        installer = self._make_installer()
        content = b"result = eval(user_input)"
        from market.installer import InstallError
        with pytest.raises(InstallError, match="eval"):
            installer._security_scan(content, "test-evil-eval")

    def test_exec_blocked(self):
        """包含 exec() 的插件应被阻断。"""
        installer = self._make_installer()
        content = b"exec('import os')"
        from market.installer import InstallError
        with pytest.raises(InstallError, match="exec"):
            installer._security_scan(content, "test-evil-exec")

    def test_os_system_blocked(self):
        """包含 os.system 的插件应被阻断。"""
        installer = self._make_installer()
        content = b"os.system('rm -rf /')"
        from market.installer import InstallError
        with pytest.raises(InstallError, match="os.system"):
            installer._security_scan(content, "test-evil-os-system")

    def test_subprocess_blocked(self):
        """包含 subprocess.Popen 的插件应被阻断。"""
        installer = self._make_installer()
        content = b"import subprocess\nsubprocess.Popen(['ls'])"
        from market.installer import InstallError
        with pytest.raises(InstallError, match="subprocess"):
            installer._security_scan(content, "test-evil-subprocess")

    def test_concat_eval_blocked(self):
        """字符串拼接 eval 混淆应被检测。"""
        installer = self._make_installer()
        content = b'x = "ev" + "al"\ngetattr(__builtins__, x)'
        from market.installer import InstallError
        with pytest.raises(InstallError):
            installer._security_scan(content, "test-evil-concat")

    def test_sensitive_path_blocked(self):
        """包含敏感路径的插件应被阻断。"""
        installer = self._make_installer()
        content = b"data = open('/etc/passwd').read()"
        from market.installer import InstallError
        with pytest.raises(InstallError, match="敏感路径"):
            installer._security_scan(content, "test-evil-path")

    def test_clean_content_passes(self):
        """正常插件内容不应被阻断。"""
        installer = self._make_installer()
        content = b"""
# normal plugin code
def hello():
    return "Hello World"

class MyPlugin:
    def on_load(self):
        print("loaded")
"""
        # 不应抛异常
        installer._security_scan(content, "test-clean")

    def test_empty_content_passes(self):
        """空内容不应被阻断。"""
        installer = self._make_installer()
        installer._security_scan(b"", "test-empty")
