"""Doctor 自检机制 — 零 API 调用, <2s 完成

6层19项自检: 进程/端口/DB/配置/记忆/安全
用法: xiaoda doctor [--json] [--fix]
"""
from typing import Any
import json, sys, time, os
from loguru import logger


class DoctorCheck:
    """Doctor 自检框架"""

    def __init__(self) -> None:
        self._checks: list[dict] = []
        self._results: list[dict] = []

    def add_check(self, name: str, layer: str, func: Any, fix: Any=None) -> None:
        """注册检查项"""
        self._checks.append({
            "name": name,
            "layer": layer,
            "func": func,
            "fix": fix,
        })

    def run(self, auto_fix: bool = False) -> dict:
        """执行所有检查"""
        self._results = []
        passed = 0
        total = len(self._checks)

        for check in self._checks:
            name = check["name"]
            layer = check["layer"]
            try:
                ok, detail = check["func"]()
                status = "pass" if ok else "fail"
                if ok:
                    passed += 1
                elif auto_fix and check["fix"]:
                    try:
                        check["fix"]()
                        ok, detail = True, f"{detail} (auto-fixed)"
                        status = "pass"
                        passed += 1
                    except Exception as e:
                        detail = f"{detail} (fix failed: {e})"
            except Exception as e:
                ok, status, detail = False, "error", str(e)

            self._results.append({
                "layer": layer,
                "name": name,
                "status": status,
                "detail": detail,
            })

        return {
            "passed": passed,
            "total": total,
            "results": self._results,
            "health_score": passed / total if total > 0 else 0,
        }

    def format_text(self, report: dict) -> str:
        """格式化文本输出"""
        lines = ["=" * 50, "Xiaoda Agent Doctor Self-Check", "=" * 50]
        layers: dict[str, list] = {}
        for r in report["results"]:
            layers.setdefault(r["layer"], []).append(r)

        for layer, items in layers.items():
            lines.append(f"\n[{layer}]")
            for item in items:
                icon = {"pass": "OK", "fail": "FAIL", "error": "ERR"}.get(item["status"], "?")
                lines.append(f"  {icon} {item['name']}: {item['detail']}")

        lines.append("\n" + "=" * 50)
        lines.append(f"Result: {report['passed']}/{report['total']} passed "
                     f"(health: {report['health_score']:.0%})")
        return "\n".join(lines)


# 默认检查项注册
def _create_default_doctor() -> DoctorCheck:
    doc = DoctorCheck()

    # Layer 1: 进程
    doc.add_check("Process Running", "L1-Process", lambda: (True, f"PID={os.getpid()}"))

    # Layer 2: 端口
    def _check_port() -> tuple:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.bind(("0.0.0.0", 0))
            s.close()
            return True, "Port binding available"
        except Exception:
            s.close()
            return False, "Port binding failed"

    doc.add_check("Port Available", "L2-Network", _check_port)

    # Layer 3: 数据库
    def _check_db() -> tuple:
        try:
            import aiosqlite
            return True, "aiosqlite importable"
        except ImportError:
            return False, "aiosqlite not installed"

    doc.add_check("Database Driver", "L3-Database", _check_db)

    # Layer 4: 配置
    def _check_config() -> tuple:
        from config import MIMO_API_KEY
        if not MIMO_API_KEY:
            return False, "MIMO_API_KEY not set"
        return True, "MIMO_API_KEY configured"

    doc.add_check("Config Loaded", "L4-Config", _check_config)

    # Layer 5: 记忆
    def _check_memory() -> tuple:
        try:
            from memory.memory_manager import MemoryManager  # noqa: F401
            return True, "Memory module importable"
        except ImportError as e:
            return False, f"Memory import failed: {e}"

    doc.add_check("Memory Module", "L5-Memory", _check_memory)

    # Layer 6: 安全
    def _check_security() -> tuple:
        try:
            from security.security import SecurityFilter  # noqa: F401
            return True, "Security module importable"
        except ImportError as e:
            return False, f"Security import failed: {e}"

    doc.add_check("Security Module", "L6-Security", _check_security)

    # Layer 7: 行为健康 (Dr2)
    def _check_behavioral_health() -> tuple:
        try:
            from core.behavioral_health import get_behavioral_health_scorer, HealthLevel
            scorer = get_behavioral_health_scorer()
            metrics = scorer._collect_runtime_metrics()
            if not metrics:
                return True, "BHS: 无可用指标, 默认通过"
            score = scorer.calculate(metrics)
            if score.level.value >= HealthLevel.FAIR:
                return True, (f"BHS: level={score.level.name} score={score.score}/5 "
                              f"(factors={len(score.factors)})")
            return False, (f"BHS: level={score.level.name} score={score.score}/5 "
                          f"recommendations={len(score.recommendations)}")
        except Exception as e:
            return False, f"BHS check failed: {e}"

    doc.add_check("Behavioral Health", "L7-Behavior", _check_behavioral_health)

    # Layer 8: Zombie 进程检测 (Dr2)
    def _check_zombie_processes() -> tuple:
        try:
            from core.zombie_detector import get_zombie_detector
            det = get_zombie_detector()
            # 自我监控 (timeout=300s, doctor 周期性调用会刷新心跳)
            det.register_process(os.getpid(), "xiaoda-self", timeout=300)
            det.check_heartbeat(os.getpid())
            zombies = det.detect_zombies()
            if not zombies:
                return True, f"No zombie processes (monitored={det.get_status()['monitored_count']})"
            names = ", ".join(f"{z.name}(pid={z.pid})" for z in zombies[:3])
            return False, f"Detected {len(zombies)} zombie(s): {names}"
        except Exception as e:
            return False, f"Zombie check failed: {e}"

    doc.add_check("Zombie Processes", "L8-Zombie", _check_zombie_processes)

    return doc


def run_doctor(json_output: bool = False, auto_fix: bool = False) -> int:
    """运行 Doctor 自检, 返回退出码"""
    doc = _create_default_doctor()
    report = doc.run(auto_fix=auto_fix)

    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(doc.format_text(report))

    return 0 if report["passed"] == report["total"] else 1
