"""WebSocket 虚空终端子模块（自 web/ws_hub.py 逐字节搬移，2026-08-25 技术债 P2）。

内容：PTY/Windows 管道终端会话全生命周期——_handle_terminal_start/input/
resize/kill、PTY 读取线程（_setup_pty_reader/_setup_win_*）、输出合帧
（_term_out_buf + _queue_term_output，~16ms/帧防 xterm 渲染冲垮）、
僵尸收割（_reap_unix_child）与清理（_cleanup_pty）。

状态单一事实源：_pty_sessions/_pty_sessions_lock/_term_out_buf 等定义于
本模块；ws_hub 门面 re-export 保持 `hub._pty_sessions` 引用面（测试以
mutate 形态使用 dict/实例属性，不重绑，故门面别名安全）。

对 ws_hub 的反向依赖（manager 发送）一律函数内延迟 import，
避免 server → ws_hub → 本模块的模块级循环。
"""
from __future__ import annotations

import asyncio
import os
import platform
import signal
import struct
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    # Windows: 使用 subprocess + 管道模拟终端
    import subprocess as _subprocess
    _HAS_PTY = False
else:
    import fcntl
    import pty
    import termios
    _HAS_PTY = True

# PTY 终端会话: term_sid -> {pid, fd, conn_id, shell, alive}
_pty_sessions: dict[str, dict] = {}
_pty_sessions_lock = threading.Lock()

# 终端输出合帧缓冲: term_sid -> {"buf": str, "timer": asyncio.TimerHandle|None}
# PTY 大输出会被内核拆成大量小块(实测 288KB/2188 次 read)，逐条发 JSON 帧
# 会把前端 xterm 渲染冲垮——按 ~16ms/帧合并后发送。
_term_out_buf: dict[str, dict] = {}
_TERM_FLUSH_INTERVAL_S = 0.016
_TERM_FLUSH_MAX_CHARS = 65536


def _try_import_winpty():
    """ConPTY 可用性探测（仅 win32 有轮子）：返回 PtyProcess 类或 None。

    Windows 会话优先 ConPTY（真终端语义：resize/TUI 全支持），
    未安装 pywinpty 时回退 subprocess 管道（无 TTY，兼容旧行为）。"""
    if os.name != "nt":
        return None
    try:
        from winpty import PtyProcess  # noqa: PLC0415 —— 平台可选依赖懒加载
        return PtyProcess
    except (ImportError, OSError):
        logger.debug("ws.winpty_unavailable: 回退管道模式")
        return None


async def _handle_terminal_start(conn_id: str, msg: dict, term_sid: str) -> None:
    from web.ws_hub import manager  # 延迟导入:避免 server→ws_hub→本模块 模块级环
    """启动一个终端会话：Linux 用 PTY，Windows 用 subprocess 管道。

    msg 字段：
      shell    — Shell 类型 (bash/zsh/python/node/cmd/powershell/wsl)，默认 bash
      cols     — 终端列数
      rows     — 终端行数
    """
    # P0(技术债审查)：sid 已存在时必须拒绝而非覆盖——否则第二个连接可抢占
    # 他人会话（旧 fd/进程成孤儿、旧 reader 向劫持者连接串输出）。
    with _pty_sessions_lock:
        if term_sid in _pty_sessions:
            logger.warning("ws.terminal.start.duplicate conn_id={} term_sid={}",
                           conn_id, term_sid)
            await manager.send_to(conn_id, {
                "type": "terminal_error", "term_sid": term_sid,
                "error": "term_sid already exists"})
            return
    shell_type = (msg.get("shell") or "bash").strip().lower()
    try:
        cols = int(msg.get("cols") or 80)
        rows = int(msg.get("rows") or 24)
        if not (2 <= cols <= 500 and 2 <= rows <= 200):
            raise ValueError
    except (TypeError, ValueError):
        cols, rows = 80, 24

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    if _HAS_PTY:
        # ── Linux / macOS: PTY 方式 ──
        shell_map = {
            "bash": "bash", "zsh": "zsh",
            "python": "python3", "node": "node",
        }
        shell_cmd = shell_map.get(shell_type, "bash")
        env["SHELL"] = shell_cmd
        loop = asyncio.get_running_loop()

        try:
            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                # ── 子进程 ──
                os.chdir(str(Path.home()))
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                os.execvpe(shell_cmd, [shell_cmd], env)
            else:
                with _pty_sessions_lock:
                    _pty_sessions[term_sid] = {
                        "pid": child_pid, "fd": master_fd, "conn_id": conn_id,
                        "shell": shell_type, "alive": True, "loop": loop,
                        "is_windows": False,
                    }
                logger.info("ws.terminal.start term_sid={} shell={} pid={}", term_sid, shell_type, child_pid)
                await manager.send_to(conn_id, {
                    "type": "terminal_started", "term_sid": term_sid, "shell": shell_type})
                _setup_pty_reader(term_sid)

        except (OSError, RuntimeError, ValueError) as e:
            logger.error("ws.terminal.start.failed term_sid={} error={}", term_sid, str(e))
            await manager.send_to(conn_id, {
                "type": "terminal_error", "term_sid": term_sid,
                "error": str(e)[:200]})
    else:
        # ── Windows: ConPTY 优先（真终端语义），缺 pywinpty 回退管道 ──
        shell_map_win = {
            "cmd": "cmd.exe",
            "powershell": "powershell.exe",
            "pwsh": "pwsh.exe",
            "python": "python.exe",
            "node": "node.exe",
            "wsl": "wsl.exe",
            "bash": "bash.exe",
        }
        exe = shell_map_win.get(shell_type, "cmd.exe")
        loop = asyncio.get_running_loop()
        PtyProcess = _try_import_winpty()

        if PtyProcess is not None:
            # ConPTY：真 PTY——resize/TUI(opencode 等)全支持
            try:
                pty_proc = PtyProcess.spawn(
                    exe, cwd=str(Path.home()), dimensions=(rows, cols),
                    env=list(f"{k}={v}" for k, v in env.items()))
                with _pty_sessions_lock:
                    _pty_sessions[term_sid] = {
                        "pid": pty_proc.pid, "winpty": pty_proc,
                        "conn_id": conn_id, "shell": shell_type,
                        "alive": True, "loop": loop,
                        "is_windows": True, "conpty": True,
                    }
                logger.info("ws.terminal.start term_sid={} shell={} pid={} mode=conpty",
                            term_sid, shell_type, pty_proc.pid)
                await manager.send_to(conn_id, {
                    "type": "terminal_started", "term_sid": term_sid,
                    "shell": shell_type, "mode": "conpty"})
                _setup_win_pty_reader(term_sid)
                return
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("ws.conpty_spawn_failed term_sid={} error={} → 回退管道",
                               term_sid, str(e)[:150])

        # 管道回退：无 TTY 语义（resize no-op、全屏 TUI 不可用）
        try:
            proc = _subprocess.Popen(
                [exe] if not exe.endswith("powershell.exe") and not exe.endswith("pwsh.exe")
                else [exe, "-NoLogo"],
                stdin=_subprocess.PIPE,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.STDOUT,
                bufsize=0,
                env=env,
                cwd=str(Path.home()),
                creationflags=_subprocess.CREATE_NEW_PROCESS_GROUP
                    if hasattr(_subprocess, "CREATE_NEW_PROCESS_GROUP") else 0,
            )
            with _pty_sessions_lock:
                _pty_sessions[term_sid] = {
                    "pid": proc.pid, "proc": proc, "conn_id": conn_id,
                    "shell": shell_type, "alive": True, "loop": loop,
                    "is_windows": True, "conpty": False,
                }
            logger.info("ws.terminal.start term_sid={} shell={} pid={} mode=pipe",
                        term_sid, shell_type, proc.pid)
            await manager.send_to(conn_id, {
                "type": "terminal_started", "term_sid": term_sid, "shell": shell_type})
            _setup_win_pipe_reader(term_sid)

        except (OSError, RuntimeError, ValueError) as e:
            logger.error("ws.terminal.start.failed term_sid={} error={}", term_sid, str(e))
            await manager.send_to(conn_id, {
                "type": "terminal_error", "term_sid": term_sid,
                "error": str(e)[:200]})


def _setup_pty_reader(term_sid: str) -> None:
    """用 loop.add_reader() 注册 PTY fd 的可读回调。"""
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
    if not session:
        return
    fd = session["fd"]
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    def _on_pty_readable() -> None:
        """当 PTY master fd 有数据可读时被调用。"""
        try:
            data = os.read(fd, 8192)
        except OSError:
            _cleanup_pty(term_sid)
            return

        if not data:
            _cleanup_pty(term_sid)
            return

        text = data.decode("utf-8", errors="replace")

        # 输出推送到前端：合帧节流（~16ms 一帧合并多次 read，防前端渲染冲垮）
        _queue_term_output(term_sid, conn_id, text)

        # 送入标记符检测器（内部按行缓冲）
        try:
            from web.pty_executor import feed_output
            feed_output(text)
        except (ImportError, OSError, RuntimeError):
            logger.debug("ws.feed_output_error", exc_info=True)

    loop.add_reader(fd, _on_pty_readable)


def _queue_term_output(term_sid: str, conn_id: str, text: str) -> None:
    from web.ws_hub import manager  # 延迟导入:避免 server→ws_hub→本模块 模块级环
    """终端输出合帧：缓冲当前块并调度 ~16ms 后的冲刷（在事件循环线程执行）。

    超过单帧上限立即冲刷，避免单条巨帧占内存。"""
    loop: asyncio.AbstractEventLoop | None = None
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if session is not None:
            loop = session.get("loop")
    if loop is None:
        return

    def _flush(term_sid: str = term_sid) -> None:
        entry = _term_out_buf.pop(term_sid, None)
        if not entry or not entry["buf"]:
            return
        sid_ = entry["conn_id"]
        asyncio.ensure_future(manager.send_to(sid_, {
            "type": "terminal_output", "term_sid": term_sid,
            "data": entry["buf"]}))

    with _pty_sessions_lock:
        entry = _term_out_buf.get(term_sid)
        if entry is None:
            entry = {"buf": "", "conn_id": conn_id, "timer": None}
            _term_out_buf[term_sid] = entry
    entry["buf"] += text
    if len(entry["buf"]) >= _TERM_FLUSH_MAX_CHARS:
        # 已满：取消定时器立即发（保持顺序——仍在循环线程串行执行）
        if entry["timer"] is not None:
            entry["timer"].cancel()
        _flush()
        return
    if entry["timer"] is None and loop is not None:
        entry["timer"] = loop.call_later(_TERM_FLUSH_INTERVAL_S, _flush)


def _setup_win_pty_reader(term_sid: str) -> None:
    """Windows ConPTY：后台线程读 PtyProcess 输出，推回事件循环（合帧）。"""
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
    if not session:
        return
    pty_proc = session["winpty"]
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    def _reader_thread() -> None:
        try:
            while pty_proc.isalive():
                # pywinpty read 在无数据时短暂阻塞，返回空串继续轮询
                data = pty_proc.read(8192)
                if not data:
                    if not pty_proc.isalive():
                        break
                    time.sleep(0.01)
                    continue
                loop.call_soon_threadsafe(
                    _queue_term_output, term_sid, conn_id, data)
        except (OSError, RuntimeError, EOFError):
            logger.debug("ws.conpty_reader_error term_sid={}", term_sid,
                         exc_info=True)
        finally:
            loop.call_soon_threadsafe(_cleanup_pty, term_sid)

    import threading
    t = threading.Thread(target=_reader_thread, daemon=True)
    t.start()


def _setup_win_pipe_reader(term_sid: str) -> None:
    """Windows: 在后台线程中读取 subprocess stdout 管道。"""
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
    if not session:
        return
    proc = session["proc"]
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    def _reader_thread() -> None:
        """后台线程：阻塞读取 stdout，推送到 event loop。"""
        try:
            while True:
                data = proc.stdout.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                loop.call_soon_threadsafe(_queue_term_output, term_sid, conn_id, text)

                # 送入标记符检测器（内部按行缓冲）
                try:
                    from web.pty_executor import feed_output
                    feed_output(text)
                except (ImportError, OSError, RuntimeError):
                    logger.debug("ws.feed_output_error_win", exc_info=True)
        except (OSError, RuntimeError):
            logger.debug("ws.win_pipe_reader_error term_sid={}", term_sid, exc_info=True)
        finally:
            loop.call_soon_threadsafe(_cleanup_pty, term_sid)

    import threading
    t = threading.Thread(target=_reader_thread, daemon=True)
    t.start()


def _reap_unix_child(pid: int) -> int:
    """线程池内执行：SIGKILL 补刀 + 有界轮询收割，返回真实退出码。

    reader 看到 EOF/EIO 时子进程可能尚未真正退出；旧实现
    waitpid(WNOHANG) 拿到 (0,0) 会被误判为 rc=0 且不再收割 → defunct 堆积。
    轮询最坏 ~300ms，必须离开事件循环线程执行（否则单进程
    WebUI+QQ+WS 共享的 loop 整体停摆）。"""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # 已退出（正常 EOF 路径常见）或无权限
    for _ in range(30):
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return -1  # 已无此子进程（被别处收割）
        if wpid == pid:
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        time.sleep(0.01)
    return -1


def _notify_terminal_exit(loop: asyncio.AbstractEventLoop, conn_id: str,
                          term_sid: str, rc: int) -> None:
    from web.ws_hub import manager  # 延迟导入:避免 server→ws_hub→本模块 模块级环
    try:
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                manager.send_to(conn_id, {
                    "type": "terminal_exit", "term_sid": term_sid, "returncode": rc
                }), loop=loop))
    except RuntimeError:
        logger.debug("ws.terminal_exit_send_failed term_sid={}", term_sid)


def _cleanup_pty(term_sid: str) -> None:
    from web.ws_hub import manager  # 延迟导入:避免 server→ws_hub→本模块 模块级环
    """清理终端会话（在 reader 回调中调用，不能 await）。

    注意：本函数只做非阻塞操作；Unix 收割下放线程池，
    terminal_exit 通知由收割完成回调发送。"""
    # 先冲刷残留输出再清缓冲，保证退出前的最后几行不丢
    entry = _term_out_buf.pop(term_sid, None)
    if entry and entry["buf"]:
        if entry.get("timer") is not None:
            entry["timer"].cancel()
        asyncio.ensure_future(manager.send_to(entry["conn_id"], {
            "type": "terminal_output", "term_sid": term_sid,
            "data": entry["buf"]}))
    with _pty_sessions_lock:
        session = _pty_sessions.pop(term_sid, None)
    if not session:
        return
    session["alive"] = False
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    if session.get("is_windows", False):
        # ── Windows: ConPTY 优先，其次 subprocess 管道 ──
        wp = session.get("winpty") if session.get("conpty") else None
        if wp is not None:
            rc = -1
            try:
                wp.terminate(force=True)
            except (OSError, RuntimeError):
                logger.debug("ws.conpty_terminate_error", exc_info=True)
            # ConPTY 无 wait 返回码语义，统一 -1（前端只显示退出提示）
        else:
            proc = session.get("proc")
            rc = -1
            if proc:
                try:
                    proc.terminate()
                    rc = proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    logger.debug("ws.process_terminate_error", exc_info=True)
                    try:
                        proc.kill()
                    except (OSError, PermissionError):
                        logger.debug("ws.process_kill_error", exc_info=True)
                    rc = -1
        _notify_terminal_exit(loop, conn_id, term_sid, rc)
        logger.info("ws.terminal.exit term_sid={} rc={}", term_sid, rc)
    else:
        # ── Unix: 关闭 PTY fd（非阻塞）；收割下放线程池 ──
        fd = session["fd"]
        try:
            loop.remove_reader(fd)
        except (OSError, ValueError):
            logger.debug("ws.remove_reader_error", exc_info=True)
        try:
            os.close(fd)
        except OSError:
            logger.debug("ws.close_fd_error", exc_info=True)

        def _reap_done(fut: "asyncio.Future[int]") -> None:
            try:
                rc = fut.result()
            except Exception:  # noqa: BLE001 —— 收割线程任何异常都不阻断通知
                rc = -1
            _notify_terminal_exit(loop, conn_id, term_sid, rc)
            logger.info("ws.terminal.exit term_sid={} rc={}", term_sid, rc)

        try:
            loop.run_in_executor(None, _reap_unix_child,
                                 session["pid"]).add_done_callback(_reap_done)
        except RuntimeError:
            # loop 已关闭（停机竞态）：退化为就地收割，不再发通知
            _reap_unix_child(session["pid"])
            logger.info("ws.terminal.exit term_sid={} rc=-1 loop_closed",
                        term_sid)


def _handle_terminal_input(conn_id: str, msg: dict) -> None:
    """将用户输入写入终端 stdin。"""
    term_sid = str(msg.get("term_sid") or "")
    data = msg.get("data", "")
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if not session or not session["alive"]:
            return
        if session.get("conn_id") != conn_id:
            logger.warning("ws.terminal_input.denied conn_id={} owner={}", conn_id, session.get("conn_id"))
            return
        # 锁内获取引用，锁外做实际写入（避免阻塞其他会话）
        is_windows = session.get("is_windows")
        proc = session.get("proc")
        fd = session.get("fd")
    try:
        if is_windows:
            if session.get("conpty"):
                wp = session.get("winpty")
                if wp is not None:
                    wp.write(data)
            elif proc and proc.stdin:
                proc.stdin.write(data.encode("utf-8", errors="replace"))
                proc.stdin.flush()
        else:
            os.write(fd, data.encode("utf-8", errors="replace"))
    except (OSError, BrokenPipeError):
        logger.debug("ws.terminal_input_write_failed conn_id={}", conn_id, exc_info=True)


def _handle_terminal_resize(conn_id: str, msg: dict) -> None:
    """调整终端窗口大小。"""
    term_sid = str(msg.get("term_sid") or "")
    cols = int(msg.get("cols") or 80)
    rows = int(msg.get("rows") or 24)
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if not session or not session["alive"]:
            return
        if session.get("conn_id") != conn_id:
            logger.warning("ws.terminal_resize.denied conn_id={} owner={}", conn_id, session.get("conn_id"))
            return
        if session.get("is_windows"):
            # ConPTY 会话支持 resize；管道回退无 TTY 概念，no-op
            wp = session.get("winpty")
            if session.get("conpty") and wp is not None:
                try:
                    wp.resize(rows, cols)
                except (OSError, RuntimeError, ValueError):
                    logger.debug("ws.conpty_resize_failed term_sid={}", term_sid,
                                 exc_info=True)
            return
        fd = session.get("fd")
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        logger.debug("ws.terminal_resize_failed conn_id={}", conn_id, exc_info=True)


def _handle_terminal_kill(conn_id: str, msg: dict) -> None:
    """终止终端会话 (复用 _cleanup_pty 确保前端收到 terminal_exit)."""
    term_sid = str(msg.get("term_sid") or "")
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if not session:
            return
        if session.get("conn_id") != conn_id:
            logger.warning("ws.terminal_kill.denied conn_id={} owner={}", conn_id, session.get("conn_id"))
            return
    _cleanup_pty(term_sid)
    logger.info("ws.terminal.kill term_sid={}", term_sid)
