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
import json
import logging
import os
import signal
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
    "ping_timeout": 5,          # 单次 HTTP 超时，秒
    "check_interval": 15,       # 探活间隔，秒
    "freeze_threshold": 60,     # 连续失败多少秒算卡死，秒
    "restart_delay": 3,         # 重启前等待端口释放，秒
    "max_restarts": 20,         # 滚动窗口内最大重启次数
    "restart_window": 600,      # 滚动窗口，秒
    "log_file": "",             # 空=只打 stdout；否则同时写文件
}


def _setup_log(log_file: str) -> logging.Logger:
    log = logging.getLogger("watchdog")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [WATCHDOG] %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as e:
            log.warning("watchdog.log_file_open_failed file=%s err=%s", log_file, e)
    return log


def _ping(url: str, timeout: int) -> bool:
    """向 /ping 端点发起 GET，成功返回 True。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(256).decode(errors="replace")
            return resp.status == 200 and '"ok"' in body
    except Exception:
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
            with __import__("contextlib").suppress(psutil.NoSuchProcess):
                child.kill()
        with __import__("contextlib").suppress(psutil.NoSuchProcess):
            parent.kill()
        log.info("watchdog.killed_proc_tree pid=%d children=%d", pid, len(children))
    except psutil.NoSuchProcess:
        log.debug("watchdog.proc_already_gone pid=%d", pid)


def _save_crash_snapshot(crash_dir: str, reason: str, restart_count: int) -> None:
    """保存崩溃快照到 crash_dir。"""
    try:
        Path(crash_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = {
            "time": ts,
            "reason": reason,
            "restart_count": restart_count,
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

    # ── 进程管理 ──────────────────────────────────────────────

    def _start(self) -> None:
        self.log.info("watchdog.start_main cmd=%s", " ".join(self.cmd))
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
            )
            self.log.info("watchdog.started pid=%d", self._proc.pid)
        except Exception as e:
            self.log.error("watchdog.start_failed err=%s", e)
            self._proc = None

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

        # 保存崩溃快照
        crash_dir = os.path.join(self.cwd, "logs", "crashes")
        _save_crash_snapshot(crash_dir, reason, restart_count)

        # 杀死旧进程
        if self._proc and self._proc.poll() is None:
            _kill_proc_tree(self._proc.pid, self.log)
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass

        time.sleep(self.cfg["restart_delay"])
        self._start()
        self._restart_history.append(time.time())
        return True

    # ── 主循环 ─────────────────────────────────────────────────

    def run(self) -> int:
        self.log.info("=" * 55)
        self.log.info("看门狗启动  cmd=%s", " ".join(self.cmd))
        self.log.info("ping_url=%s  check_interval=%ds  freeze_threshold=%ds",
                      self.cfg["ping_url"], self.cfg["check_interval"],
                      self.cfg["freeze_threshold"])
        self.log.info("max_restarts=%d/%ds",
                      self.cfg["max_restarts"], self.cfg["restart_window"])
        self.log.info("=" * 55)

        self._start()

        # 等主进程初始化
        time.sleep(self.cfg["check_interval"])

        last_ok = time.time()

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

            # 2. HTTP 探活
            ok = _ping(self.cfg["ping_url"], self.cfg["ping_timeout"])
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
    cfg["check_interval"] = args.check_interval
    cfg["freeze_threshold"] = args.freeze_threshold
    cfg["max_restarts"] = args.max_restarts
    cfg["log_file"] = args.log_file

    return Watchdog(cmd, cwd, cfg).run()
