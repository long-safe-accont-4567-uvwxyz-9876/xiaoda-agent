"""认证 Token 并发安全静态验证测试

通过源码静态分析验证 logout / revoke-all 路径下 _tokens 字典的并发访问安全性。
原 Bug：logout 中 _tokens.pop() 和 revoke_all 中 _tokens.clear() 未在锁保护下执行，
并发调用时可能导致 OrderedDict 数据结构损坏或 RuntimeError。
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTH_FILE = ROOT / "web" / "routers" / "auth.py"


def _read_auth_source() -> str:
    return AUTH_FILE.read_text(encoding="utf-8")


def test_logout_pop_inside_tokens_lock():
    """logout 函数中的 _tokens.pop 必须在 _tokens_lock 上下文管理器内。

    触发场景：并发调用 logout + validate_token 时，若无锁保护，
    OrderedDict 的并发修改可能导致 RuntimeError 或数据结构损坏。

    验证方法：
    - 定位 logout 函数定义
    - 在 logout 函数体内查找 _tokens.pop 调用
    - 检查该调用是否被 with _tokens_lock: 块包裹
    """
    src = _read_auth_source()
    lines = src.split("\n")

    logout_start = None
    for i, line in enumerate(lines):
        if "async def logout(" in line:
            logout_start = i
            break

    assert logout_start is not None, "未找到 logout 函数定义"

    logout_end = len(lines)
    for i in range(logout_start + 1, len(lines)):
        if lines[i].startswith("async def ") or lines[i].startswith("@router"):
            logout_end = i
            break

    logout_body = "\n".join(lines[logout_start:logout_end])

    assert "_tokens.pop" in logout_body, "logout 中应包含 _tokens.pop 调用"

    pop_line_idx = None
    for i in range(logout_start, logout_end):
        if "_tokens.pop" in lines[i]:
            pop_line_idx = i
            break

    assert pop_line_idx is not None, "未找到 _tokens.pop 所在行"

    lock_found = False
    for i in range(pop_line_idx - 1, logout_start - 1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("with _tokens_lock:"):
            lock_found = True
            break
        if stripped and not stripped.startswith("#") and not stripped.startswith("with "):
            indent_diff = len(lines[pop_line_idx]) - len(lines[pop_line_idx].lstrip())
            current_indent = len(lines[i]) - len(lines[i].lstrip())
            if current_indent < indent_diff:
                break

    assert lock_found, (
        "logout 中的 _tokens.pop 必须在 with _tokens_lock: 保护下执行，"
        "否则并发时会损坏 OrderedDict"
    )


def test_revoke_all_tokens_access_inside_lock():
    """revoke_all 函数中的 _tokens.keys() 和 _tokens.clear() 必须在锁内。

    触发场景：并发调用 revoke_all + validate_token/login 时，若无锁保护，
    list(_tokens.keys()) 和 _tokens.clear() 可能导致迭代时修改字典的 RuntimeError。
    """
    src = _read_auth_source()
    lines = src.split("\n")

    revoke_start = None
    for i, line in enumerate(lines):
        if "async def revoke_all(" in line:
            revoke_start = i
            break

    assert revoke_start is not None, "未找到 revoke_all 函数定义"

    revoke_end = len(lines)
    for i in range(revoke_start + 1, len(lines)):
        if lines[i].startswith("async def ") or (lines[i].startswith("@router") and i > revoke_start + 1):
            revoke_end = i
            break

    revoke_body = "\n".join(lines[revoke_start:revoke_end])

    assert "_tokens.clear()" in revoke_body, "revoke_all 中应包含 _tokens.clear() 调用"
    assert "_tokens.keys()" in revoke_body, "revoke_all 中应包含 _tokens.keys() 调用"

    clear_line_idx = None
    for i in range(revoke_start, revoke_end):
        if "_tokens.clear()" in lines[i]:
            clear_line_idx = i
            break

    assert clear_line_idx is not None, "未找到 _tokens.clear() 所在行"

    lock_found = False
    for i in range(clear_line_idx - 1, revoke_start - 1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("with _tokens_lock:"):
            lock_found = True
            break
        if stripped and not stripped.startswith("#"):
            clear_indent = len(lines[clear_line_idx]) - len(lines[clear_line_idx].lstrip())
            current_indent = len(lines[i]) - len(lines[i].lstrip())
            if current_indent < clear_indent:
                break

    assert lock_found, (
        "revoke_all 中的 _tokens.clear() 必须在 with _tokens_lock: 保护下执行"
    )


def test_validate_token_tokens_write_inside_lock():
    """_validate_token 中对 _tokens 的写入操作必须在锁内。"""
    src = _read_auth_source()
    lines = src.split("\n")

    validate_start = None
    for i, line in enumerate(lines):
        if "def _validate_token(" in line:
            validate_start = i
            break

    assert validate_start is not None, "未找到 _validate_token 函数定义"

    validate_end = len(lines)
    for i in range(validate_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("def ") and "def _" not in stripped[:5]:
            validate_end = i
            break
        if stripped.startswith("@") and i > validate_start + 3:
            validate_end = i
            break

    write_lines = []
    for i in range(validate_start, validate_end):
        if "_tokens[" in lines[i] and "=" in lines[i]:
            write_lines.append(i)

    assert len(write_lines) > 0, "_validate_token 中应有对 _tokens 的写入"

    for line_idx in write_lines:
        lock_found = False
        for i in range(line_idx - 1, validate_start - 1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("with _tokens_lock:"):
                lock_found = True
                break
            if stripped and not stripped.startswith("#"):
                write_indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
                current_indent = len(lines[i]) - len(lines[i].lstrip())
                if current_indent < write_indent:
                    break
        assert lock_found, (
            f"_validate_token 中第 {line_idx+1} 行的 _tokens 写入必须在锁保护下"
        )
