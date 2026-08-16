"""MCP server command/env 安全策略 — 二进制白名单 + env 键名黑名单。

从 web/routers/mcp.py 抽取的中性模块（VULN-24 扩展）：
- web 层：HTTPException(400) 包装后对外暴露
- market 安装层：市场 manifest 的 connections 字段解析后同样必须经过本模块
  校验，防止投毒 manifest 在安装时注入任意命令（RCE）。

本模块不得 import web.* / market.*（避免循环依赖），校验失败一律抛 ValueError。
"""
from __future__ import annotations

import os
from typing import Any

# VULN-24：MCP command 二进制白名单
_ALLOWED_MCP_BINARIES = frozenset({
    "node", "npx", "uvx", "uv", "python", "python3",
    "deno", "bun",
})

# VULN-24 扩展：env 键名黑名单前缀。
# 除动态链接器注入（LD_/DYLD_）与解释器配置（PYTHON*、PATH）外，补充各运行时
# 的"进程启动即执行/加载"类环境变量，防止经 npx/node/uv 等白名单二进制注入代码：
#   NODE_OPTIONS (--require=/tmp/x.js)、BASH_ENV、ENV、ZDOTDIR（shell 启动脚本）、
#   PERL5OPT、RUBYOPT、GODEBUG、GIT_*（GIT_CONFIG_*/GIT_SSH 等）、
#   JAVA_TOOL_OPTIONS / _JAVA_OPTIONS（JVM agent 注入）。
# VULN-30 补充：BASH_FUNC_*（bash 函数导出注入）、GLIBC_TUNABLES（glibc 运行时
# 参数）、SHELLOPTS / PROMPT_COMMAND / IFS / PS4（shell 选项与命令注入）。
# 注意：匹配逻辑为 upper == prefix or upper.startswith(prefix)，"ENV" 会误伤
# "ENVIRONMENT" 之类键——这是可接受的 fail-closed 行为，保留现状。
_ENV_BLOCKED_PREFIXES = (
    "LD_", "DYLD_", "PYTHON", "PATH",
    "NODE_OPTIONS", "BASH_ENV", "ENV", "ZDOTDIR",
    "PERL5OPT", "RUBYOPT", "GODEBUG", "GIT_",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    "BASH_FUNC_", "GLIBC_TUNABLES",
    "SHELLOPTS", "PROMPT_COMMAND", "IFS", "PS4",
)

# shell 元字符（与历史检查保持一致，扩大范围后拒绝一切拼接执行）
_SHELL_METACHARS = ("|", "&", ";", "`", "$(", "${", "<", ">", "\n", "\r")


def _basename(command: str) -> str:
    """提取命令的 basename（兼容完整路径）"""
    return os.path.basename(command)


def validate_mcp_command(command: Any) -> None:
    """校验 MCP server command 是否为允许的二进制。

    fail-closed：不在白名单或含 shell 元字符即拒绝。
    校验失败抛 ValueError（调用方自行决定映射为 HTTPException 或 InstallError）。
    """
    if not command or not isinstance(command, str):
        raise ValueError("command 不能为空")
    # 拒绝 shell 元字符（与现有检查保持一致，扩大范围）
    if any(c in command for c in _SHELL_METACHARS):
        raise ValueError("command 包含非法字符（shell 元字符）")
    cmd_name = _basename(command.strip())
    if not cmd_name:
        raise ValueError("command 不能为空")
    # 白名单比对
    if cmd_name.lower() not in _ALLOWED_MCP_BINARIES:
        raise ValueError(
            f"command '{cmd_name}' 不在允许的二进制列表内"
            f"（允许: {', '.join(sorted(_ALLOWED_MCP_BINARIES))}）",
        )


def validate_mcp_env(env: Any) -> None:
    """校验 MCP server env 键名，拒绝危险前缀注入。

    校验失败抛 ValueError（调用方自行决定映射为 HTTPException 或 InstallError）。
    """
    if not env:
        return
    if not isinstance(env, dict):
        # 市场 manifest 可能给任意类型（字符串/列表等），fail-closed 拒绝
        raise ValueError("env 必须是键值对对象")
    for key in env:
        upper = str(key).upper()
        for prefix in _ENV_BLOCKED_PREFIXES:
            if upper == prefix or upper.startswith(prefix):
                raise ValueError(
                    f"env 键 '{key}' 包含禁止的前缀 '{prefix}'"
                    "（禁止注入 LD_* / PYTHON* / PATH / DYLD_* 等）",
                )
