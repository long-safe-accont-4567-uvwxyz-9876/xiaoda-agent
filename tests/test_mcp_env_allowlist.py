"""TDD 测试：MCP env 键名黑名单补全（VULN-24 扩展，RCE 风险）。

攻击向量：白名单内二进制（npx/node/uv…）启动时读取 NODE_OPTIONS / BASH_ENV /
ENV / ZDOTDIR / PERL5OPT / RUBYOPT / GODEBUG / GIT_* / JAVA_TOOL_OPTIONS 等
环境变量并执行代码。黑名单必须覆盖这些前缀，同时放行 API_KEY / GITHUB_TOKEN
等正常密钥类键。
"""
import pytest
from fastapi import HTTPException

from security.mcp_command_policy import validate_mcp_env, _ENV_BLOCKED_PREFIXES
from web.routers.mcp import _validate_mcp_env


# ── 新增前缀的代表性危险键（每个前缀至少一个）───────────────────

@pytest.mark.parametrize("key", [
    # 原有前缀（回归保障）
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PATH",
    # 新增：Node.js 启动即执行 --require 脚本
    "NODE_OPTIONS",
    # 新增：shell 启动脚本注入
    "BASH_ENV",
    "ENV",
    "ZDOTDIR",
    # 新增：解释器启动注入
    "PERL5OPT",
    "RUBYOPT",
    "GODEBUG",
    # 新增：Git 子进程注入（GIT_CONFIG_* / GIT_SSH / GIT_EXEC_PATH 等）
    "GIT_CONFIG_COUNT",
    "GIT_SSH",
    # 新增：JVM agent 注入
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
])
def test_reject_new_dangerous_env_keys(key):
    """每个新增前缀的代表性键必须被策略层拒绝（ValueError）。"""
    with pytest.raises(ValueError):
        validate_mcp_env({key: "evil"})


def test_reject_new_dangerous_env_keys_via_web_router():
    """Web 路由包装层必须转成 HTTPException(400)。"""
    with pytest.raises(HTTPException) as exc_info:
        _validate_mcp_env({"NODE_OPTIONS": "--require=/tmp/x.js"})
    assert exc_info.value.status_code == 400


# ── 正常键必须放行 ────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "API_KEY",
    "GITHUB_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "BRAVE_API_KEY",
    "MODEL",
    "NODE_PATH",
    "HOME",
])
def test_allow_normal_env_keys(key):
    """正常密钥类 env 键应放行。"""
    # 不应抛异常
    validate_mcp_env({key: "secret"})


def test_env_blocked_prefixes_cover_required_set():
    """黑名单至少覆盖任务要求的全部前缀。"""
    required = {
        "LD_", "DYLD_", "PYTHON", "PATH", "NODE_OPTIONS",
        "BASH_ENV", "ENV", "ZDOTDIR", "PERL5OPT", "RUBYOPT",
        "GODEBUG", "GIT_", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    }
    assert required <= set(_ENV_BLOCKED_PREFIXES)
