"""TDD 测试：命令审批白名单拒绝含 shell 元字符的命令（VULN-25）。"""
import pytest

from security.permission_manager import PermissionManager


# shell 元字符集合（与 permission_manager 内部一致）
_SHELL_METACHARS = set("|&;`$()<>{}[]!?*~\n\r")


@pytest.fixture
def pm():
    return PermissionManager()


class TestWhitelistMetacharRejection:
    def test_reject_semicolon_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("ls; rm -rf /")

    def test_reject_pipe_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("ls | cat")

    def test_reject_ampersand_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("ls &")

    def test_reject_backtick_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("`id`")

    def test_reject_dollar_paren_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("$(id)")

    def test_reject_redirect_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("ls > /etc/passwd")

    def test_allow_simple_cmd_name(self, pm):
        """纯命令名应正常入白名单"""
        pm.add_to_whitelist("ls")
        assert "ls" in pm.get_whitelist()

    def test_allow_cmd_with_args_no_metachar(self, pm):
        """含参数但无 shell 元字符应正常入白名单（只存命令名）"""
        pm.add_to_whitelist("npm install axios")
        assert "npm" in pm.get_whitelist()
        assert "install" not in pm.get_whitelist()

    def test_reject_newline_in_whitelist(self, pm):
        with pytest.raises(ValueError, match="shell 元字符"):
            pm.add_to_whitelist("ls\nrm -rf /")