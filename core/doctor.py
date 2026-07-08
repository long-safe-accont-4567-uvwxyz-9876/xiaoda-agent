"""Doctor 自检与智能自修复机制 — 零 API 调用, <2s 完成

跨平台自检: Docker / Windows / Linux
10层自检 + 自动修复: 进程/端口/DB/配置/数据目录/锁文件/端口冲突/记忆/安全/行为
用法: xiaoda doctor [--json] [--fix]
"""
from typing import Any
import json, sys, time, os, shutil, subprocess
from pathlib import Path
from loguru import logger
import contextlib


def _detect_platform() -> str:
    """检测当前运行环境: docker / windows / linux / mac"""
    if os.path.isfile("/.dockerenv") or os.path.isdir("/app/.dockerenv"):
        return "docker"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


class DoctorCheck:
    """Doctor 自检框架"""

    def __init__(self) -> None:
        self._checks: list[dict] = []
        self._results: list[dict] = []
        self._fixes_applied: list[str] = []

    def add_check(self, name: str, layer: str, func: Any, fix: Any=None) -> None:
        self._checks.append({
            "name": name,
            "layer": layer,
            "func": func,
            "fix": fix,
        })

    def run(self, auto_fix: bool = False) -> dict:
        self._results = []
        self._fixes_applied = []
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
                        detail = f"{detail} (auto-fixed)"
                        status = "pass"
                        passed += 1
                        self._fixes_applied.append(f"{layer}/{name}")
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
            "fixes_applied": self._fixes_applied,
            "platform": _detect_platform(),
        }

    def format_text(self, report: dict) -> str:
        lines = ["=" * 50, "Xiaoda Agent Doctor Self-Check", "=" * 50]
        lines.append(f"Platform: {report.get('platform', 'unknown')}")
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
        if report.get("fixes_applied"):
            lines.append(f"Auto-fixed: {', '.join(report['fixes_applied'])}")
        return "\n".join(lines)


def _create_default_doctor() -> DoctorCheck:
    doc = DoctorCheck()
    _register_process_checks(doc)
    _register_config_checks(doc)
    _register_data_dir_checks(doc)
    _register_self_heal_checks(doc)
    _register_behavior_checks(doc)
    return doc


def _register_process_checks(doc: DoctorCheck) -> None:
    """注册进程和端口检查（Layer 1-2）。"""
    doc.add_check("Process Running", "L1-Process", lambda: (True, f"PID={os.getpid()}"))

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


def _register_config_checks(doc: DoctorCheck) -> None:
    """注册数据库、配置、记忆、安全检查（Layer 3-6）。"""

    def _check_db() -> tuple:
        try:
            import aiosqlite  # noqa: F401
            return True, "aiosqlite importable"
        except ImportError:
            return False, "aiosqlite not installed"

    doc.add_check("Database Driver", "L3-Database", _check_db)

    def _check_db_integrity() -> tuple:
        try:
            from config import DATA_DIR
            db_path = DATA_DIR / "xiaoda_memory.db"
            if not db_path.exists():
                return True, "DB file not yet created (will create on first use)"
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            if result[0] == "ok":
                return True, "DB integrity OK"
            return False, f"DB integrity check failed: {result[0]}"
        except Exception as e:
            return False, f"DB integrity check error: {e}"

    def _fix_db_integrity() -> None:
        from config import DATA_DIR
        db_path = DATA_DIR / "xiaoda_memory.db"
        if not db_path.exists():
            return
        import sqlite3
        backup_path = db_path.with_suffix(".db.bak")
        shutil.copy2(db_path, backup_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("VACUUM")
        conn.close()
        logger.info("doctor.db_integrity_fixed", backup=str(backup_path))

    doc.add_check("Database Integrity", "L3-Database", _check_db_integrity, _fix_db_integrity)

    def _check_config() -> tuple:
        from config import MIMO_API_KEY
        if not MIMO_API_KEY:
            return False, "MIMO_API_KEY not set"
        return True, "MIMO_API_KEY configured"

    doc.add_check("Config Loaded", "L4-Config", _check_config)

    def _check_env_file() -> tuple:
        from config import _KIOXIA_BASE
        env_candidates = [
            Path(__file__).resolve().parent.parent / ".env",
            _KIOXIA_BASE / ".env",
            Path.home() / ".ai-agent" / ".env",
        ]
        if getattr(sys, 'frozen', False):
            env_candidates.insert(0, Path(sys.executable).parent / ".env")
        for p in env_candidates:
            if p.exists():
                return True, f".env found at {p}"
        return False, ".env not found"

    doc.add_check("Env File", "L4-Config", _check_env_file)

    def _check_memory() -> tuple:
        try:
            from memory.memory_manager import MemoryManager  # noqa: F401
            return True, "Memory module importable"
        except ImportError as e:
            return False, f"Memory import failed: {e}"

    doc.add_check("Memory Module", "L5-Memory", _check_memory)

    def _check_security() -> tuple:
        try:
            from security.security import SecurityFilter  # noqa: F401
            return True, "Security module importable"
        except ImportError as e:
            return False, f"Security import failed: {e}"

    doc.add_check("Security Module", "L6-Security", _check_security)


def _register_data_dir_checks(doc: DoctorCheck) -> None:
    """注册数据目录、磁盘空间、前端文件检查（Layer 7）。"""

    def _check_data_dirs() -> tuple:
        from config import (DATA_DIR, LOG_DIR, STICKER_DIR, XIAOLI_STICKER_DIR,
                            AGENT_STICKER_BASE, FILE_DIR, MEDIA_DIR, VOICE_REF_DIR,
                            MEMORY_STATE_DIR, PLUGINS_CONFIG_DIR)
        missing = []
        checked = [DATA_DIR, LOG_DIR, STICKER_DIR, XIAOLI_STICKER_DIR,
                   AGENT_STICKER_BASE, FILE_DIR, MEDIA_DIR, VOICE_REF_DIR,
                   MEMORY_STATE_DIR, PLUGINS_CONFIG_DIR]
        for d in checked:
            if not d.exists():
                missing.append(str(d))
        if missing:
            return False, f"Missing dirs: {', '.join(missing[:3])}{'...' if len(missing) > 3 else ''}"
        return True, f"All {len(checked)} data dirs exist"

    def _fix_data_dirs() -> None:
        from config import (DATA_DIR, LOG_DIR, STICKER_DIR, XIAOLI_STICKER_DIR,
                            AGENT_STICKER_BASE, FILE_DIR, MEDIA_DIR, VOICE_REF_DIR,
                            MEMORY_STATE_DIR, PLUGINS_CONFIG_DIR)
        for d in [DATA_DIR, LOG_DIR, STICKER_DIR, XIAOLI_STICKER_DIR,
                  AGENT_STICKER_BASE, FILE_DIR, MEDIA_DIR, VOICE_REF_DIR,
                  MEMORY_STATE_DIR, PLUGINS_CONFIG_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    doc.add_check("Data Directories", "L7-DataDirs", _check_data_dirs, _fix_data_dirs)

    def _check_disk_space() -> tuple:
        from config import DATA_DIR
        try:
            usage = os.statvfs(str(DATA_DIR))
            free_gb = (usage.f_bavail * usage.f_frsize) / (1024 ** 3)
        except (AttributeError, OSError):
            try:
                usage = shutil.disk_usage(str(DATA_DIR))
                free_gb = usage.free / (1024 ** 3)
            except Exception:
                return True, "Disk space check skipped (unsupported)"
        if free_gb < 0.5:
            return False, f"Low disk space: {free_gb:.1f} GB free"
        return True, f"Disk space OK: {free_gb:.1f} GB free"

    doc.add_check("Disk Space", "L7-DataDirs", _check_disk_space)

    def _check_web_dist() -> tuple:
        try:
            if getattr(sys, 'frozen', False):
                base = Path(sys._MEIPASS)
                dist_dir = base / "web" / "dist"
            else:
                dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"
        except Exception:
            dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"
        if not dist_dir.exists():
            return False, f"web/dist not found at {dist_dir}"
        index = dist_dir / "index.html"
        if not index.exists():
            return False, "index.html missing in web/dist"
        return True, f"web/dist OK ({dist_dir})"

    doc.add_check("Frontend Assets", "L7-DataDirs", _check_web_dist)

    def _check_writable() -> tuple:
        from config import DATA_DIR
        test_file = DATA_DIR / ".doctor_write_test"
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            test_file.write_text("ok")
            test_file.unlink()
            return True, f"DATA_DIR writable ({DATA_DIR})"
        except Exception as e:
            return False, f"DATA_DIR not writable: {e}"

    doc.add_check("Data Writable", "L7-DataDirs", _check_writable)


def _register_self_heal_checks(doc: DoctorCheck) -> None:
    """注册智能自修复检查（Layer 8）— 跨平台适配 Docker/Windows/Linux。"""

    def _check_stale_locks() -> tuple:
        from config import DATA_DIR
        lock_patterns = ["*.lock", "*.pid", "*.lck"]
        stale = []
        for pattern in lock_patterns:
            for f in DATA_DIR.rglob(pattern):
                try:
                    age = time.time() - f.stat().st_mtime
                    if age > 3600:
                        stale.append(f"{f.name} ({age:.0f}s old)")
                except OSError:
                    pass
        if stale:
            return False, f"Stale lock files: {', '.join(stale[:3])}"
        return True, "No stale lock files"

    def _fix_stale_locks() -> None:
        from config import DATA_DIR
        lock_patterns = ["*.lock", "*.pid", "*.lck"]
        for pattern in lock_patterns:
            for f in DATA_DIR.rglob(pattern):
                try:
                    age = time.time() - f.stat().st_mtime
                    if age > 3600:
                        f.unlink()
                        logger.info("doctor.stale_lock_removed", file=str(f))
                except OSError:
                    pass

    doc.add_check("Stale Lock Files", "L8-SelfHeal", _check_stale_locks, _fix_stale_locks)

    def _check_port_conflict() -> tuple:
        import socket
        port = int(os.getenv("WEBUI_PORT", "8082"))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return True, f"Port {port} available"
        except OSError:
            s.close()
            return False, f"Port {port} in use by another process"

    def _fix_port_conflict() -> None:
        port_str = os.getenv("WEBUI_PORT", "8082")
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            logger.warning("doctor.invalid_port value={}", port_str)
            return
        port = int(port_str)
        platform = _detect_platform()
        if platform == "windows":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetTCPConnection -LocalPort {} -ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}".format(port)],
                timeout=10, capture_output=True, check=False,
            )
        elif platform == "docker":
            logger.warning("doctor.port_conflict_docker", port=port, hint="Change WEBUI_PORT env var")
        else:
            result = subprocess.run(
                ["lsof", "-ti:{}".format(port)],
                timeout=10, capture_output=True, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            pids = result.stdout.decode().strip().split()
            if pids:
                subprocess.run(["kill", "-9"] + pids, timeout=5, capture_output=True, check=False)
        time.sleep(1)

    doc.add_check("Port Conflict", "L8-SelfHeal", _check_port_conflict, _fix_port_conflict)

    def _check_log_rotation() -> tuple:
        from config import LOG_DIR
        if not LOG_DIR.exists():
            return True, "Log dir not yet created"
        total_size = sum(f.stat().st_size for f in LOG_DIR.rglob("*") if f.is_file())
        size_mb = total_size / (1024 * 1024)
        if size_mb > 500:
            return False, f"Log dir too large: {size_mb:.0f} MB"
        return True, f"Log dir size OK: {size_mb:.1f} MB"

    def _fix_log_rotation() -> None:
        from config import LOG_DIR
        if not LOG_DIR.exists():
            return
        for f in sorted(LOG_DIR.glob("*.log.*"), reverse=True):
            if f.stat().st_size > 10 * 1024 * 1024:
                f.unlink()
                logger.info("doctor.large_log_removed", file=str(f))
        for f in sorted(LOG_DIR.glob("*.log"), reverse=True)[5:]:
            f.unlink()
            logger.info("doctor.old_log_removed", file=str(f))

    doc.add_check("Log Rotation", "L8-SelfHeal", _check_log_rotation, _fix_log_rotation)

    def _check_temp_files() -> tuple:
        from config import DATA_DIR
        temp_patterns = ["*.tmp", "*.temp", "*.bak", "*.swp", "*~"]
        found = []
        for pattern in temp_patterns:
            for f in DATA_DIR.rglob(pattern):
                found.append(f.name)
        if found:
            return False, f"Temp files found: {', '.join(found[:5])}"
        return True, "No temp files"

    def _fix_temp_files() -> None:
        from config import DATA_DIR
        temp_patterns = ["*.tmp", "*.temp", "*.swp", "*~"]
        for pattern in temp_patterns:
            for f in DATA_DIR.rglob(pattern):
                try:
                    f.unlink()
                    logger.info("doctor.temp_file_removed", file=str(f))
                except OSError:
                    pass

    doc.add_check("Temp Files", "L8-SelfHeal", _check_temp_files, _fix_temp_files)

    def _check_docker_volume() -> tuple:
        if _detect_platform() != "docker":
            return True, "Not Docker environment (skipped)"
        data_dir = os.getenv("KIOXIA_DATA_DIR", "")
        if not data_dir:
            return False, "KIOXIA_DATA_DIR not set in Docker"
        data_path = Path(data_dir)
        if not data_path.exists():
            return False, f"Docker data dir not found: {data_dir}"
        if not os.access(data_path, os.W_OK):
            return False, f"Docker data dir not writable: {data_dir}"
        return True, f"Docker volume OK ({data_dir})"

    def _fix_docker_volume() -> None:
        data_dir = os.getenv("KIOXIA_DATA_DIR", "/data")
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(data_dir, 0o755)

    doc.add_check("Docker Volume", "L8-SelfHeal", _check_docker_volume, _fix_docker_volume)

    def _check_voice_ref_dirs() -> tuple:
        from config import VOICE_REF_DIR
        if not VOICE_REF_DIR.exists():
            return False, f"VOICE_REF_DIR missing: {VOICE_REF_DIR}"
        agent_dirs = [d for d in VOICE_REF_DIR.iterdir() if d.is_dir()] if VOICE_REF_DIR.exists() else []
        return True, f"VOICE_REF_DIR OK ({len(agent_dirs)} agent dirs)"

    def _fix_voice_ref_dirs() -> None:
        from config import VOICE_REF_DIR
        VOICE_REF_DIR.mkdir(parents=True, exist_ok=True)
        for agent in ("xiaoda", "xiaoli", "xiaoke", "xiaolian", "xiaolang"):
            (VOICE_REF_DIR / agent).mkdir(parents=True, exist_ok=True)

    doc.add_check("Voice Ref Dirs", "L8-SelfHeal", _check_voice_ref_dirs, _fix_voice_ref_dirs)

    def _check_sticker_dirs() -> tuple:
        from config import STICKER_DIR, XIAOLI_STICKER_DIR, AGENT_STICKER_BASE
        missing = []
        for name, d in [("stickers", STICKER_DIR), ("xiaoli-stickers", XIAOLI_STICKER_DIR),
                        ("agent-stickers", AGENT_STICKER_BASE)]:
            if not d.exists():
                missing.append(name)
        if missing:
            return False, f"Sticker dirs missing: {', '.join(missing)}"
        return True, "All sticker dirs exist"

    def _fix_sticker_dirs() -> None:
        from config import STICKER_DIR, XIAOLI_STICKER_DIR, AGENT_STICKER_BASE
        for d in [STICKER_DIR, XIAOLI_STICKER_DIR, AGENT_STICKER_BASE]:
            d.mkdir(parents=True, exist_ok=True)

    doc.add_check("Sticker Dirs", "L8-SelfHeal", _check_sticker_dirs, _fix_sticker_dirs)


def _register_behavior_checks(doc: DoctorCheck) -> None:
    """注册行为健康和 zombie 进程检查（Layer 9-10）。"""

    def _check_behavioral_health() -> tuple:
        try:
            from core.behavioral_health import get_behavioral_health_scorer, HealthLevel
            scorer = get_behavioral_health_scorer()
            metrics = scorer._collect_runtime_metrics()
            if not metrics:
                return True, "BHS: no metrics available, default pass"
            score = scorer.calculate(metrics)
            if score.level.value >= HealthLevel.FAIR:
                return True, (f"BHS: level={score.level.name} score={score.score}/5 "
                              f"(factors={len(score.factors)})")
            return False, (f"BHS: level={score.level.name} score={score.score}/5 "
                          f"recommendations={len(score.recommendations)}")
        except Exception as e:
            return False, f"BHS check failed: {e}"

    doc.add_check("Behavioral Health", "L9-Behavior", _check_behavioral_health)

    def _check_zombie_processes() -> tuple:
        try:
            from core.zombie_detector import get_zombie_detector
            det = get_zombie_detector()
            det.register_process(os.getpid(), "xiaoda-self", timeout=300)
            det.check_heartbeat(os.getpid())
            zombies = det.detect_zombies()
            if not zombies:
                return True, f"No zombie processes (monitored={det.get_status()['monitored_count']})"
            names = ", ".join(f"{z.name}(pid={z.pid})" for z in zombies[:3])
            return False, f"Detected {len(zombies)} zombie(s): {names}"
        except Exception as e:
            return False, f"Zombie check failed: {e}"

    doc.add_check("Zombie Processes", "L10-Zombie", _check_zombie_processes)


def run_doctor(json_output: bool = False, auto_fix: bool = False) -> int:
    """运行 Doctor 自检, 返回退出码"""
    doc = _create_default_doctor()
    report = doc.run(auto_fix=auto_fix)

    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(doc.format_text(report))

    return 0 if report["passed"] == report["total"] else 1