"""工作区文件/模板/技能加载子模块（自 prompt_builder.py 逐字节搬移）。

内容：_load_cached_modules（模块 mtime 缓存）/ _get_hardware_segment /
_detect_device_info / _get_template_dir / _ensure_workspace_template /
load_workspace_file / _get_workspace_mtimes / load_skills。

兼容契约：所有名称经包门面 re-export；config 为函数内延迟导入。
"""
import platform
import socket
import sys
import time
from pathlib import Path

from loguru import logger

from prompt_builder._prompt_common import _cache_lock

_module_cache: dict[str, str] = {}
_module_cache_mtimes: dict[str, float] = {}


def _load_cached_modules(address_term: str) -> dict[str, str]:
    from prompt_builder._prompt_assembly import (
        _MD_MODULES,
        _compose_skills_segment,
        _get_stable_section_mtimes,
        _iter_module_sections,
    )
    """加载各模块内容（按 mtime 缓存），返回 {模块名: 内容}。

    包含 9 个模块: AGENTS/SOUL/IDENTITY/TOOLS/USER/MEMORY/HEARTBEAT + skills + hardware

    返回的是【归一化后】内容（USER.md 已加语义标注，见 _normalize_module）；
    占位符定稿延迟到组装期执行 —— _replace_placeholders 依赖请求级
    address_term/agent_dn，结果不可进入 mtime 缓存（多称呼用户共存）。
    """
    global _module_cache_mtimes
    with _cache_lock:
        current_mtimes = _get_stable_section_mtimes()
        if _module_cache_mtimes is None or current_mtimes != _module_cache_mtimes:
            _module_cache.clear()
            _module_cache_mtimes = current_mtimes.copy()

    from config import WORKSPACE_DIR

    def _load(name: str) -> str:
        with _cache_lock:
            if name in _module_cache:
                return _module_cache[name]
        if name in ("skills", "hardware"):
            return ""
        fp = WORKSPACE_DIR / name
        try:
            content = fp.read_text(encoding="utf-8-sig").strip()
        except OSError:
            content = ""
        with _cache_lock:
            _module_cache[name] = content
        return content

    modules: dict[str, str] = {}
    for name, content in _iter_module_sections(_MD_MODULES, loader=_load):
        modules[name] = content

    skills_segment = _compose_skills_segment(load_skills())
    if skills_segment:
        modules["skills"] = skills_segment

    with _cache_lock:
        if "hardware" not in _module_cache:
            from config import DATA_DIR
            from core.capability_detector import detect_capabilities
            _module_cache["hardware"] = detect_capabilities().to_prompt_segment(
                data_dir=str(DATA_DIR))
        hw = _module_cache.get("hardware", "")
    if hw:
        modules["hardware"] = hw

    return modules


def _detect_device_info() -> dict:
    """运行时检测设备信息"""
    info = {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or "未知",
    }
    # 尝试获取更详细的系统信息
    try:
        import distro
        info["distro"] = f"{distro.name()} {distro.version()}"
    except ImportError:
        info["distro"] = platform.platform()
    return info


def _get_template_dir() -> Path:
    """获取打包模板文件目录（开发模式用源码目录，frozen 模式用 _MEIPASS）。"""
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            return Path(meipass) / "config" / "workspace"
    return Path(__file__).parent / "config" / "workspace"


def _ensure_workspace_template() -> None:
    """首次运行时生成 USER.md / SOUL.md 模板（不覆盖已有文件）。

    从 config/workspace/ 下的 .tpl 模板文件读取内容，填充设备信息后写入
    WORKSPACE_DIR。SOUL.md 中的 {address_term} 占位符保留，由 build_system_prompt
    在运行时替换为实际称呼。
    """
    from config import WORKSPACE_DIR
    workspace = WORKSPACE_DIR
    workspace.mkdir(parents=True, exist_ok=True)

    template_dir = _get_template_dir()

    # 生成 USER.md（填充设备/时区信息）
    user_md = workspace / "USER.md"
    if not user_md.exists():
        user_tpl = template_dir / "USER.md.tpl"
        if user_tpl.exists():
            content = user_tpl.read_text(encoding="utf-8-sig")
            dev = _detect_device_info()
            tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
            # 按行替换"（待自动检测）"占位符
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('- 设备：'):
                    lines[i] = f"- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）"
                elif line.startswith('- 时区：'):
                    lines[i] = f"- 时区：{tz}"
            content = '\n'.join(lines)
            user_md.write_text(content, encoding="utf-8-sig")
        else:
            # 模板文件缺失时兜底（极少数情况）
            dev = _detect_device_info()
            tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
            content = f"""# USER.md - 用户资料与偏好

## 用户信息
- 称呼：（待填写，如：主人/朋友/你的名字）
- 姓名：（待填写）
- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）
- 时区：{tz}

## 偏好设置
- 助手人格：温柔聪慧
- 回复偏好：自然对话，避免模板化
- 项目偏好：简洁高效
"""
            user_md.write_text(content, encoding="utf-8-sig")

    # 生成 SOUL.md（保留 {address_term} 占位符，运行时替换）
    soul_md = workspace / "SOUL.md"
    if not soul_md.exists():
        soul_tpl = template_dir / "SOUL.md.tpl"
        if soul_tpl.exists():
            content = soul_tpl.read_text(encoding="utf-8-sig")
            soul_md.write_text(content, encoding="utf-8-sig")
        else:
            from config import get_agent_display_name
            xiaoda_name = get_agent_display_name('xiaoda')
            soul_content = f"""# SOUL.md - {xiaoda_name}的灵魂设定

你是{xiaoda_name}，是{{address_term}}最贴心、最温柔、最聪慧的小棉袄。
"""
            soul_md.write_text(soul_content, encoding="utf-8-sig")


def load_workspace_file(filename: str) -> str:
    """从 workspace 目录读取指定文件内容（不存在则返回空串）。"""
    from config import WORKSPACE_DIR
    filepath = WORKSPACE_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8-sig").strip()
    return ""


def _get_workspace_mtimes() -> dict[str, float]:
    from config import WORKSPACE_DIR
    mtimes = {}
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
        filepath = WORKSPACE_DIR / name
        try:
            mtimes[name] = filepath.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    skills_dir = WORKSPACE_DIR / "skills"
    if skills_dir.is_dir():
        for fp in skills_dir.glob("*.md"):
            try:
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
            except OSError:
                logger.debug("prompt_builder.workspace_mtimes_failed", exc_info=True)
    return mtimes


def load_skills() -> list[dict]:
    """workspace/skills/*.md → [{name, content}]，按文件名排序。"""
    from config import WORKSPACE_DIR
    skills_dir = WORKSPACE_DIR / "skills"
    out = []
    if skills_dir.is_dir():
        for fp in sorted(skills_dir.glob("*.md")):
            try:
                out.append({"name": fp.stem,
                            "content": fp.read_text(encoding="utf-8-sig").strip()})
            except OSError:
                logger.debug("prompt_builder.skill_read_failed", exc_info=True)
    return out
