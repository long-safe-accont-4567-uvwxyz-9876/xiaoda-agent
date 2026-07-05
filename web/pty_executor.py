"""PTY 命令执行器 —— 让 Agent 通过虚空终端执行 Shell 命令。

当小妲调用 shell_command 工具时，如果存在活跃的终端会话，
命令会注入到 PTY 中执行，用户在终端面板中实时看到命令输入和输出。

原理：
  1. 发送命令 + ANSI dim 标记 → stty echo
  2. 标记用 ANSI dim + 背景色渲染，在终端中几乎不可见
  3. PTY 读取器将原始输出送入 feed_output，内部按行检测标记
  4. asyncio.Event 通知调用方输出完成
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

# ANSI dim + 与终端背景色相近 → 几乎不可见
_DIM = "\033[2m"
_BG = "\033[38;2;6;14;10m"   # #060e0a = 终端背景色
_RST = "\033[0m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


@dataclass
class CommandState:
    """一个待完成的命令的状态。"""
    marker_id: str
    collecting: bool = False
    output_lines: list[str] = field(default_factory=list)
    line_buf: str = ""
    exit_code: int = -1
    event: asyncio.Event = field(default_factory=asyncio.Event)


# 当前正在等待输出的命令（同一时间最多一个）
_pending_cmd: Optional[CommandState] = None


async def execute_on_pty(
    conn_id: str,
    command: str,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """在活跃的终端会话中执行命令，返回 (success, output)。

    如果没有活跃的终端会话，返回 (False, "") 让调用方 fallback 到 subprocess。
    """
    global _pending_cmd

    from web.ws_hub import _pty_sessions

    # 找到任意活跃的终端会话
    session = None
    for _sid, sess in _pty_sessions.items():
        if sess.get("alive"):
            session = sess
            break

    if not session:
        return False, ""

    # 如果正在执行另一个命令，等待它完成
    if _pending_cmd and not _pending_cmd.event.is_set():
        try:
            await asyncio.wait_for(_pending_cmd.event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            _pending_cmd = None

    # 生成唯一标记
    marker_id = uuid.uuid4().hex[:10]
    state = CommandState(marker_id=marker_id)
    _pending_cmd = state

    # 标记格式（ANSI dim + 背景色，终端中几乎不可见）：
    #   开始：\033[2m\033[38;2;6;14;10m_A_{id}_\033[0m
    #   结束：\033[2m\033[38;2;6;14;10m_Z_{id}_{exitcode}_\033[0m
    is_win = session.get("is_windows", False)
    _start_marker = f"_A_{marker_id}_"
    _end_marker = f"_Z_{marker_id}_"

    if is_win:
        # PowerShell / CMD：用 Write-Host 输出隐藏标记
        # PowerShell 用 $LASTEXITCODE 获取退出码
        pty_input = (
            f"Write-Host -NoNewline '{_DIM}{_BG}{_start_marker}{_RST}'\n"
            f"{command}\n"
            f"$_ec = $LASTEXITCODE; if ($null -eq $_ec) {{ $_ec = 0 }}\n"
            f"Write-Host '{_DIM}{_BG}{_end_marker}{_RST}'\"$_ec\"\n"
        )
    else:
        pty_input = (
            f"echo -e '{_DIM}{_BG}{_start_marker}{_RST}'\n"
            f"{command}\n"
            f"__ec=$?\n"
            f"echo -e '{_DIM}{_BG}{_end_marker}\"${{__ec}}\"{_RST}'\n"
            f"stty echo 2>/dev/null\n"
        )

    # 写入 PTY
    try:
        if is_win:
            proc = session.get("proc")
            if proc and proc.stdin:
                proc.stdin.write(pty_input.encode("utf-8"))
                proc.stdin.flush()
        else:
            fd = session["fd"]
            os.write(fd, pty_input.encode("utf-8"))
    except (OSError, BrokenPipeError) as e:
        _pending_cmd = None
        logger.error("pty_executor.write_failed error={}", str(e))
        return False, ""

    # 等待输出完成或超时
    try:
        await asyncio.wait_for(state.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _pending_cmd = None
        logger.warning("pty_executor.timeout cmd='{}' marker={}", command[:60], marker_id)
        return True, "[命令执行超时]\n" + "\n".join(state.output_lines)

    _pending_cmd = None

    output = "\n".join(state.output_lines).strip()
    return True, output


def feed_output(text: str) -> None:
    """PTY 读取器每读到一块输出时调用，内部按行缓冲并检测标记。"""
    global _pending_cmd

    state = _pending_cmd
    if not state:
        return

    marker_id = state.marker_id
    start_pat = f"_A_{marker_id}_"
    end_pat = f"_Z_{marker_id}_"

    state.line_buf += text

    while "\n" in state.line_buf:
        raw_line, state.line_buf = state.line_buf.split("\n", 1)
        clean = _ANSI_RE.sub("", raw_line)

        if not state.collecting:
            if start_pat in clean:
                state.collecting = True
            continue

        if end_pat in clean:
            state.collecting = False
            idx = clean.find(end_pat)
            before = clean[:idx]
            if before:
                state.output_lines.append(before)
            after = clean[idx + len(end_pat):]
            exit_str = after.strip().strip('"').rstrip("_")
            try:
                state.exit_code = int(exit_str)
            except (ValueError, IndexError):
                state.exit_code = -1
            if state.exit_code != 0:
                state.output_lines.append(f"[exit code: {state.exit_code}]")
            state.event.set()
            return

        state.output_lines.append(clean)

    # 标记可能跨 chunk，对不完整行也做检测
    if state.line_buf:
        clean_buf = _ANSI_RE.sub("", state.line_buf)
        if not state.collecting and start_pat in clean_buf:
            after_start = clean_buf[clean_buf.find(start_pat) + len(start_pat):]
            if after_start:
                state.output_lines.append(after_start)
            state.collecting = True
            state.line_buf = ""
        elif state.collecting and end_pat in clean_buf:
            idx = clean_buf.find(end_pat)
            before = clean_buf[:idx]
            if before:
                state.output_lines.append(before)
            after = clean_buf[idx + len(end_pat):]
            exit_str = after.strip().strip('"').rstrip("_")
            try:
                state.exit_code = int(exit_str)
            except (ValueError, IndexError):
                state.exit_code = -1
            if state.exit_code != 0:
                state.output_lines.append(f"[exit code: {state.exit_code}]")
            state.collecting = False
            state.line_buf = ""
            state.event.set()


# 向后兼容：保留旧接口名
feed_output_line = feed_output
