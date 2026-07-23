"""
utils/watchdog_runner.py

跨平台看门狗守护进程，通过 HTTP /ping 探活，
卡死超时后强制杀死并重启主进程。

设计原则：
- 不干扰业务逻辑，仅在进程卡死/崩溃时介入
- 零第三方依赖（仅 stdlib + psutil）
- Windows / Linux / macOS 通用
- 通过 agent.py watchdog 子命令启动
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# ──────────────────────────────────────────────────────────────
# 默认配置
# ──────────────────────────────────────────────────────────────
DEFAULTS = {
    "ping_url": "http://127.0.0.1:8082/api/v1/ping",
    "host": "127.0.0.1",         # 端口释放检测用
    "port": 8082,                # 端口释放检测用
    "ping_timeout": 5,           # 单次 HTTP 超时，秒
    "ping_retries": 3,           # 探活重试次数（I1: 避免单次抖动误判）
    "check_interval": 15,        # 探活间隔，秒
    "freeze_threshold": 60,      # 连续失败多少秒算卡死，秒
    "restart_delay": 3,          # 重启前等待端口释放，秒
    "max_restarts": 20,          # 滚动窗口内最大重启次数
    "restart_window": 600,       # 滚动窗口，秒
    "start_fail_backoff": 30,    # W2: 启动失败后退避秒数
    "log_file": "",              # 空=只打 stdout；否则同时写文件
    "log_max_bytes": 10 * 1024 * 1024,  # I3: 日志轮转上限
    "log_backup_count": 5,       # I3: 日志保留份数
}


def _setup_log(log_file: str) -> logging.Logger:
    log = logging.getLogger("watchdog")
    log.setLevel(logging.DEBUG)
    # 避免重复添加 handler（多次调用时）
    if log.handlers:
        return log
    fmt = logging.Formatter("%(asctime)s [WATCHDOG] %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            # I3: 改用 RotatingFileHandler 防止日志无限增长
            fh = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=DEFAULTS["log_max_bytes"],
                backupCount=DEFAULTS["log_backup_count"],
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as e:
            log.warning("watchdog.log_file_open_failed file=%s err=%s", log_file, e)
    return log


def _ping_once(url: str, timeout: int) -> bool:
    """向 /ping 端点发起单次 GET，成功返回 True。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(256).decode(errors="replace")
            return resp.status == 200 and '"ok"' in body
    except Exception:
        return False


def _ping(url: str, timeout: int, retries: int = 1) -> bool:
    """I1: 带重试的 HTTP 探活，避免单次网络抖动触发误判。"""
    for attempt in range(retries):
        if _ping_once(url, timeout):
            return True
        if attempt < retries - 1:
            time.sleep(2)  # 重试间隔 2 秒
    return False


def _kill_proc_tree(pid: int, log: logging.Logger) -> None:
    """终止进程及其所有子进程。"""
    if not _HAS_PSUTIL:
        # fallback：直接 kill 主进程
        try:
            if sys.platform == "win32":
                subprocess.call(["taskkill", "/F", "/PID", str(pid), "/T"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        with contextlib.suppress(psutil.NoSuchProcess):
            parent.kill()
        log.info("watchdog.killed_proc_tree pid=%d children=%d", pid, len(children))
    except psutil.NoSuchProcess:
        log.debug("watchdog.proc_already_gone pid=%d", pid)


def _wait_port_release(host: str, port: int, timeout: int = 10) -> bool:
    """W3: 等待端口释放，返回是否成功。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                if result != 0:
                    return True  # 端口已释放
        except Exception:
            return True  # 无法检测时假设已释放
        time.sleep(1)
    return False


def _collect_proc_diag(pid: int | None) -> dict:
    """I2: 采集进程诊断信息（CPU/内存/线程数等）。"""
    if not _HAS_PSUTIL or pid is None:
        return {"pid": pid, "psutil_available": _HAS_PSUTIL}
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            return {
                "pid": pid,
                "cpu_percent": p.cpu_percent(interval=0.1),
                "memory_mb": round(p.memory_info().rss / 1024 / 1024, 2),
                "num_threads": p.num_threads(),
                "num_fds": p.num_handles() if sys.platform == "win32" else p.num_fds(),
                "status": p.status(),
                "create_time": datetime.fromtimestamp(p.create_time()).strftime("%Y-%m-%d %H:%M:%S"),
            }
    except (psutil.NoSuchProcess, Exception):
        return {"pid": pid, "error": "process_gone_or_unavailable"}


def _save_crash_snapshot(crash_dir: str, reason: str, restart_count: int,
                         pid: int | None = None) -> None:
    """保存崩溃快照到 crash_dir（I2: 含进程诊断信息）。"""
    try:
        Path(crash_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = {
            "time": ts,
            "reason": reason,
            "restart_count": restart_count,
            "process_diag": _collect_proc_diag(pid),  # I2: 进程诊断信息
        }
        snap_path = Path(crash_dir) / f"snapshot_{ts}.json"
        snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class Watchdog:
    def __init__(self, cmd: list[str], cwd: str, cfg: dict) -> None:
        self.cmd = cmd
        self.cwd = cwd
        self.cfg = cfg
        self.log = _setup_log(cfg.get("log_file", ""))
        self._proc: subprocess.Popen | None = None
        self._restart_history: list[float] = []
        self._running = True
        self._install_signal_handlers()  # W1: 信号处理

    # ── W1: 信号处理 ─────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        """注册 SIGTERM/SIGINT 信号处理，优雅退出。"""
        # Windows 不支持 SIGTERM（用 SIGBREAK 替代）
        signals = [signal.SIGINT]
        if sys.platform != "win32":
            signals.append(signal.SIGTERM)
        else:
            with contextlib.suppress(AttributeError):
                signals.append(signal.SIGBREAK)

        for sig in signals:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        self.log.info("watchdog.signal_received signum=%d — 正在优雅退出", signum)
        self._running = False

    # ── 进程管理 ──────────────────────────────────────────────

    def _start(self) -> bool:
        """W2: 启动主进程，返回是否成功。"""
        self.log.info("watchdog.start_main cmd=%s", " ".join(self.cmd))
        try:
            # I4: 重定向 stdio，避免子进程继承父进程的标准输入输出
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log.info("watchdog.started pid=%d", self._proc.pid)
            return True
        except Exception as e:
            self.log.error("watchdog.start_failed err=%s", e)
            self._proc = None
            return False

    def _stop(self, timeout: int = 10) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            return
        self.log.info("watchdog.stopping pid=%d", self._proc.pid)
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.log.warning("watchdog.graceful_timeout force_kill pid=%d", self._proc.pid)
                _kill_proc_tree(self._proc.pid, self.log)
                self._proc.wait(timeout=5)
        except OSError:
            pass

    def _restart(self, reason: str) -> bool:
        """重启主进程，返回是否成功（超限则 False）。"""
        now = time.time()
        window = self.cfg["restart_window"]
        max_r = self.cfg["max_restarts"]
        self._restart_history = [t for t in self._restart_history if now - t < window]

        if len(self._restart_history) >= max_r:
            self.log.critical(
                "watchdog.restart_limit_exceeded count=%d window=%ds — 停止自动恢复，请人工排查",
                len(self._restart_history), window,
            )
            return False

        restart_count = len(self._restart_history) + 1
        self.log.warning("watchdog.restart reason=%s count=%d", reason, restart_count)

        old_pid = self._proc.pid if self._proc else None

        # 保存崩溃快照（I2: 含进程诊断信息）
        crash_dir = os.path.join(self.cwd, "logs", "crashes")
        _save_crash_snapshot(crash_dir, reason, restart_count, pid=old_pid)

        # 杀死旧进程
        if self._proc and self._proc.poll() is None:
            _kill_proc_tree(self._proc.pid, self.log)
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass

        # W3: 等待端口释放
        host = self.cfg.get("host", "127.0.0.1")
        port = self.cfg.get("port", 8082)
        if not _wait_port_release(host, port, timeout=10):
            self.log.error("watchdog.port_not_released host=%s port=%d — 仍尝试启动", host, port)

        time.sleep(self.cfg["restart_delay"])
        if not self._start():  # W2: 启动失败处理
            self.log.error("watchdog.restart_start_failed — 退避 %ds 后由主循环重试",
                           self.cfg["start_fail_backoff"])
            time.sleep(self.cfg["start_fail_backoff"])
        self._restart_history.append(time.time())
        return True

    # ── 主循环 ─────────────────────────────────────────────────

    def run(self) -> int:
        self.log.info("=" * 55)
        self.log.info("看门狗启动  cmd=%s", " ".join(self.cmd))
        self.log.info("ping_url=%s  check_interval=%ds  freeze_threshold=%ds",
                      self.cfg["ping_url"], self.cfg["check_interval"],
                      self.cfg["freeze_threshold"])
        self.log.info("max_restarts=%d/%ds  ping_retries=%d",
                      self.cfg["max_restarts"], self.cfg["restart_window"],
                      self.cfg["ping_retries"])
        self.log.info("=" * 55)

        # W2: 首次启动失败时退避重试
        if not self._start():
            self.log.error("watchdog.initial_start_failed — 退避 %ds 后重试",
                           self.cfg["start_fail_backoff"])
            time.sleep(self.cfg["start_fail_backoff"])
            if not self._start():
                self.log.critical("watchdog.initial_start_failed_twice — 退出")
                return 1

        # 等主进程初始化
        time.sleep(self.cfg["check_interval"])

        last_ok = time.time()
        ping_retries = self.cfg.get("ping_retries", 1)

        while self._running:
            # 1. 进程已退出 → 直接重启
            if self._proc is not None and self._proc.poll() is not None:
                exit_code = self._proc.poll()
                self.log.error("watchdog.proc_exited exit_code=%d", exit_code)
                if not self._restart("proc_exited"):
                    break
                last_ok = time.time()
                time.sleep(self.cfg["check_interval"])
                continue

            # 2. HTTP 探活（I1: 带重试）
            ok = _ping(self.cfg["ping_url"], self.cfg["ping_timeout"], retries=ping_retries)
            if ok:
                last_ok = time.time()
            else:
                frozen_secs = time.time() - last_ok
                self.log.debug("watchdog.ping_fail frozen_secs=%.0f threshold=%d",
                               frozen_secs, self.cfg["freeze_threshold"])
                if frozen_secs >= self.cfg["freeze_threshold"]:
                    self.log.error("watchdog.freeze_detected frozen=%.0fs — 触发重启", frozen_secs)
                    if not self._restart("freeze"):
                        break
                    last_ok = time.time()

            time.sleep(self.cfg["check_interval"])

        self._stop()
        self.log.info("watchdog.exit")
        return 0


# ──────────────────────────────────────────────────────────────
# CLI 入口（由 agent.py watchdog 子命令调用）
# ──────────────────────────────────────────────────────────────

def run_watchdog_cli(argv: list[str] | None = None) -> int:
    """解析参数并运行看门狗，供 agent.py watchdog 子命令调用。"""
    p = argparse.ArgumentParser(prog="watchdog", description="Xiaoda Agent 看门狗")
    p.add_argument("--port", type=int, default=8082, help="主进程端口（默认 8082）")
    p.add_argument("--host", type=str, default="127.0.0.1")
    p.add_argument("--mode", choices=["web", "desktop"], default="web")
    p.add_argument("--check-interval", type=int, default=DEFAULTS["check_interval"])
    p.add_argument("--freeze-threshold", type=int, default=DEFAULTS["freeze_threshold"])
    p.add_argument("--max-restarts", type=int, default=DEFAULTS["max_restarts"])
    p.add_argument("--ping-retries", type=int, default=DEFAULTS["ping_retries"],
                   help="探活重试次数（避免单次抖动误判）")
    p.add_argument("--log-file", type=str, default="")
    args = p.parse_args(argv)

    # 构造要守护的命令（与 agent.py 直接运行保持一致）
    if getattr(sys, "frozen", False):
        # PyInstaller exe 模式
        exe = sys.executable
        cmd = [exe, f"--{args.mode}", "--host", args.host, "--port", str(args.port)]
    else:
        cmd = [sys.executable, "agent.py", f"--{args.mode}",
               "--host", args.host, "--port", str(args.port)]

    cwd = str(Path(__file__).parent.parent)

    cfg = dict(DEFAULTS)
    cfg["ping_url"] = f"http://{args.host}:{args.port}/api/v1/ping"
    cfg["host"] = args.host
    cfg["port"] = args.port
    cfg["check_interval"] = args.check_interval
    cfg["freeze_threshold"] = args.freeze_threshold
    cfg["max_restarts"] = args.max_restarts
    cfg["ping_retries"] = args.ping_retries
    cfg["log_file"] = args.log_file

    return Watchdog(cmd, cwd, cfg).run()
