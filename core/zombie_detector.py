"""Zombie 进程检测 — Dr2 P1 Doctor

Zombie 进程定义: 长时间无响应但未退出, 或重复执行相同操作。

三类检测策略:
1. 心跳超时: 超过 timeout 未收到心跳 → 进程可能卡死
2. 重复行为: 在 N 次活动中都做相同的事 → 可能死循环
3. 资源不增长: CPU/内存使用长时间无变化 → 可能空转

检测目标:
- 自身进程 (os.getpid())
- 子进程 (psutil / /proc, 优先使用 psutil)

用法:
    det = ZombieDetector()
    det.register_process(pid=1234, name="agent_worker", timeout=30)
    det.check_heartbeat(1234)
    det.record_activity(1234, "tool:search")
    zombies = det.detect_zombies()
    for z in zombies:
        print(z.name, z.reason)
        det.kill_zombie(z.pid)
"""
from __future__ import annotations

import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass
class ZombieProcess:
    """检测到的 Zombie 进程"""
    pid: int
    name: str
    reason: str                              # 检测原因 (可能多条, 用 '; ' 分隔)
    last_activity: str = ""                  # 最后一次活动
    duration: float = 0.0                    # 异常持续时间 (秒)


class ZombieDetector:
    """Zombie 进程检测器"""

    # 默认重复行为阈值: 连续 N 次相同活动视为可疑死循环
    DEFAULT_REPETITION_THRESHOLD = 5

    def __init__(self, repetition_threshold: int = DEFAULT_REPETITION_THRESHOLD) -> None:
        self._processes: dict[int, dict] = {}
        self._repetition_threshold = max(1, repetition_threshold)

    # ── 注册与心跳 ──

    def register_process(self, pid: int, name: str, timeout: float) -> None:
        """注册进程监控

        如果 pid 已存在, 视为刷新 (重置状态)
        """
        now = time.time()
        self._processes[pid] = {
            "name": name,
            "timeout": float(timeout),
            "heartbeat": now,
            "activities": deque(maxlen=100),
            "last_cpu": None,         # 上次 CPU 采样
            "last_mem": None,         # 上次内存采样
            "last_change_ts": now,    # 上次资源变化时间
        }
        logger.debug(f"ZombieDetector.register pid={pid} name={name} timeout={timeout}s")

    def check_heartbeat(self, pid: int) -> bool:
        """检查/更新进程心跳

        Returns:
            True 表示进程已注册并更新心跳; False 表示未注册
        """
        info = self._processes.get(pid)
        if not info:
            return False
        info["heartbeat"] = time.time()
        return True

    def record_activity(self, pid: int, activity: str) -> None:
        """记录进程活动 (用于死循环检测)"""
        info = self._processes.get(pid)
        if not info:
            return
        info["activities"].append((activity, time.time()))

    # ── 检测 ──

    def _detect_heartbeat_timeout(self, pid: int, info: dict, now: float) -> Optional[str]:
        """检测心跳超时"""
        elapsed = now - info["heartbeat"]
        if elapsed > info["timeout"]:
            return f"心跳超时: {elapsed:.1f}s > {info['timeout']:.1f}s"
        return None

    def _detect_repetitive_activity(self, pid: int, info: dict) -> Optional[str]:
        """检测重复行为 (连续 N 次相同活动)"""
        activities = list(info["activities"])
        if len(activities) < self._repetition_threshold:
            return None
        recent = activities[-self._repetition_threshold:]
        names = [a[0] for a in recent]
        if all(n == names[0] for n in names):
            return (f"重复行为: 连续 {self._repetition_threshold} 次相同活动 "
                    f"[{names[0]}]")
        return None

    def _detect_resource_stall(self, pid: int, info: dict, now: float) -> Optional[str]:
        """检测资源不增长 (CPU/内存长时间无变化)

        需要多次采样, 首次调用仅记录基线。
        """
        try:
            import psutil
        except ImportError:
            return None
        try:
            if not psutil.pid_exists(pid):
                return None
            proc = psutil.Process(pid)
            # interval=None 表示非阻塞, 返回自上次调用以来的 CPU 百分比
            cpu = proc.cpu_percent(interval=None)
            mem = proc.memory_info().rss

            if info["last_cpu"] is not None and info["last_mem"] is not None:
                if cpu == 0.0 and mem == info["last_mem"]:
                    elapsed = now - info["last_change_ts"]
                    if elapsed > info["timeout"]:
                        return (f"资源不增长: CPU=0% 内存不变, "
                                f"持续 {elapsed:.1f}s")
                else:
                    # 资源有变化, 更新基线时间
                    info["last_change_ts"] = now
            info["last_cpu"] = cpu
            info["last_mem"] = mem
        except Exception as e:
            logger.debug(f"ZombieDetector.resource_stall_failed pid={pid}: {e}")
        return None

    def detect_zombies(self) -> list[ZombieProcess]:
        """检测 zombie 进程

        Returns:
            list[ZombieProcess], 每个被检测到的 zombie 包含 pid/name/reason/...
        """
        zombies: list[ZombieProcess] = []
        now = time.time()
        for pid, info in list(self._processes.items()):
            reasons: list[str] = []

            # 1. 心跳超时
            hb = self._detect_heartbeat_timeout(pid, info, now)
            if hb:
                reasons.append(hb)

            # 2. 重复行为
            rep = self._detect_repetitive_activity(pid, info)
            if rep:
                reasons.append(rep)

            # 3. 资源不增长 (仅 psutil 可用且 pid 存在时生效)
            stall = self._detect_resource_stall(pid, info, now)
            if stall:
                reasons.append(stall)

            if reasons:
                last_act = ""
                if info["activities"]:
                    last_act = info["activities"][-1][0]
                # 异常持续时间: 距离上次心跳的时长
                duration = now - info["heartbeat"]
                zombies.append(ZombieProcess(
                    pid=pid,
                    name=info["name"],
                    reason="; ".join(reasons),
                    last_activity=last_act,
                    duration=duration,
                ))
                logger.warning(
                    f"ZombieDetector.zombie_detected pid={pid} "
                    f"name={info['name']} reasons={reasons}"
                )
        return zombies

    # ── 终止 ──

    def kill_zombie(self, pid: int) -> bool:
        """终止 zombie 进程 (需要权限)

        发送 SIGTERM, 成功后从监控中移除。
        Returns:
            True 表示成功发送信号; False 表示权限不足/进程不存在/未注册
        """
        info = self._processes.get(pid)
        if not info:
            logger.warning(f"ZombieDetector.kill_zombie: pid={pid} not registered")
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info(f"ZombieDetector.kill_zombie SIGTERM pid={pid} name={info['name']}")
            self._processes.pop(pid, None)
            return True
        except ProcessLookupError:
            logger.warning(f"ZombieDetector.kill_zombie pid={pid} not found, removing")
            self._processes.pop(pid, None)
            return False
        except PermissionError:
            logger.error(f"ZombieDetector.kill_zombie pid={pid} permission denied")
            return False
        except Exception as e:
            logger.error(f"ZombieDetector.kill_zombie pid={pid} failed: {e}")
            return False

    # ── 状态查询 ──

    def get_status(self) -> dict:
        """获取监控状态"""
        return {
            "monitored_count": len(self._processes),
            "pids": list(self._processes.keys()),
            "repetition_threshold": self._repetition_threshold,
        }

    def unregister_process(self, pid: int) -> None:
        """取消进程监控"""
        self._processes.pop(pid, None)


# 全局单例
_detector: Optional[ZombieDetector] = None


def get_zombie_detector() -> ZombieDetector:
    """获取全局 ZombieDetector 单例"""
    global _detector
    if _detector is None:
        _detector = ZombieDetector()
    return _detector
