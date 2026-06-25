"""系统提示词构建模块。

从 config.py 拆分而来，负责构建 system prompt（含安全化版本）以及
工作区文件的读取与模板初始化。

为避免循环导入（config.py 末尾会 `from prompt_builder import *`），
本模块对 config 常量（WORKSPACE_DIR、DATA_DIR 等）采用函数内部延迟导入。
"""
import os
import time
import platform
import socket
from pathlib import Path

from loguru import logger


# ── system prompt 缓存变量 ────────────────────────────────────
_SYSTEM_PROMPT_CACHE: str = ""
_SYSTEM_PROMPT_CACHE_TS: float = 0.0
_SYSTEM_PROMPT_CACHE_TTL: float = 60.0
_SYSTEM_PROMPT_CACHE_MTIMES: dict[str, float] = {}
_SYSTEM_PROMPT_CACHE_ADDR_TERM: str = ""

# ── 非主人安全化 system prompt 缓存变量（防隐私泄露） ──────────
_SAFE_PROMPT_CACHE: str | None = None
_SAFE_PROMPT_CACHE_TS: float = 0.0

# ── P6: 增量上下文构建（稳定段缓存） ──────────────────────────
# 稳定段只随 address_term 变化，缓存计算结果；动态段每次构建
# mtime 校验：编辑 SOUL.md/AGENTS.md 等文件后自动失效缓存
_stable_prompt_cache: dict = {}
_stable_prompt_cache_mtimes: dict = None


def _get_stable_section_mtimes() -> dict[str, float]:
    """获取稳定段文件的 mtime 指纹，用于缓存失效判断。"""
    from config import WORKSPACE_DIR
    mtimes: dict[str, float] = {}
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md"):
        fp = WORKSPACE_DIR / name
        try:
            mtimes[name] = fp.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    # skills 目录
    try:
        skills_dir = WORKSPACE_DIR / "skills"
        if skills_dir.exists():
            for fp in sorted(skills_dir.glob("*.md")):
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
    except OSError:
        pass
    return mtimes


def _build_stable_prompt(address_term: str) -> str:
    """构建系统提示「稳定段」：SOUL.md/AGENTS.md/IDENTITY.md/TOOLS.md/skills/硬件信息。

    这些内容不随请求变化，只随 address_term 变化，因此用模块级 dict 缓存。
    缓存通过 workspace 文件 mtime 失效：编辑任意稳定段文件后，下次调用重新构建。
    """
    global _stable_prompt_cache_mtimes
    # 获取当前稳定段文件的 mtime 指纹
    current_mtimes = _get_stable_section_mtimes()
    if _stable_prompt_cache_mtimes is None or current_mtimes != _stable_prompt_cache_mtimes:
        # mtime 变化（或首次调用），清空整个缓存
        _stable_prompt_cache.clear()
        _stable_prompt_cache_mtimes = current_mtimes

    cache_key = address_term
    if cache_key in _stable_prompt_cache:
        return _stable_prompt_cache[cache_key]

    sections = []

    agents_rules = load_workspace_file("AGENTS.md")
    if agents_rules:
        sections.append(agents_rules)

    soul = load_workspace_file("SOUL.md")
    if soul:
        if "{address_term}" in soul:
            soul = soul.replace("{address_term}", address_term)
        sections.append(soul)

    identity = load_workspace_file("IDENTITY.md")
    if identity:
        sections.append(identity)

    tools_rules = load_workspace_file("TOOLS.md")
    if tools_rules:
        sections.append(tools_rules)

    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            sections.append("[已安装的 Skills]\n\n" + skill_texts)

    # 硬件上下文（稳定，不随请求变化）
    from config import DATA_DIR
    _npu_status = "NPU视觉识别已启用" if os.getenv("ENABLE_NPU", "").lower() in ("1", "true", "yes") else "视觉识别（ncnn后端）"
    _uname = platform.uname()
    _hostname = socket.gethostname()
    hw_context = (
        "[本机硬件信息]\n"
        f"主机名: {_hostname} | 架构: {_uname.machine} | 处理器: {_uname.processor or '未知'}\n"
        f"系统: {_uname.system} {_uname.release} ({_uname.machine})\n"
        "可用接口: GPIO (40pin排针) / I2C / SPI / UART / PWM\n"
        "可用工具: gpio_control(引脚控制) / i2c_comm(I2C通信) / hardware_status(硬件监控) / service_manage(服务管理) / network_diag(网络诊断) / dev_assist(开发辅助) / camera_capture(拍照) / vision_analyze(视觉分析)\n"
        f"数据存储: {DATA_DIR}\n"
        f"摄像头: Q8 HD Webcam (/dev/video0) | 视觉模型: YOLOv10-nano (ncnn CPU) | {_npu_status}"
    )
    sections.append(hw_context)

    result = "\n\n---\n\n".join(sections)
    _stable_prompt_cache[cache_key] = result
    return result


def _build_dynamic_prompt(extra_context: str = "") -> str:
    """构建系统提示「动态段」：USER.md/MEMORY.md/HEARTBEAT.md/extra_context。

    每次请求可能变化，不缓存。
    """
    sections = []

    user = load_workspace_file("USER.md")
    if user:
        sections.append(user)

    memory = load_workspace_file("MEMORY.md")
    if memory:
        sections.append(memory)

    heartbeat = load_workspace_file("HEARTBEAT.md")
    if heartbeat:
        sections.append(heartbeat)

    result = "\n\n---\n\n".join(sections)

    if extra_context:
        if result:
            result += f"\n\n---\n\n{extra_context}"
        else:
            result = extra_context

    return result


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
    import sys
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            return Path(meipass) / "config" / "workspace"
    return Path(__file__).parent / "config" / "workspace"


def _ensure_workspace_template():
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
            soul_content = """# SOUL.md - 纳西妲的灵魂设定

你是纳西妲，是{address_term}最贴心、最温柔、最聪慧的小棉袄。
"""
            soul_md.write_text(soul_content, encoding="utf-8-sig")


def load_workspace_file(filename: str) -> str:
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
                pass
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
                pass
    return out


def build_system_prompt(extra_context: str = "", address_term: str = "爸爸") -> str:
    # P6: 增量上下文构建路径 —— 稳定段缓存 + 动态段每次构建
    try:
        from config import PROMPT_CACHING_ENABLED
    except ImportError:
        PROMPT_CACHING_ENABLED = False

    if PROMPT_CACHING_ENABLED:
        try:
            stable = _build_stable_prompt(address_term)
            dynamic = _build_dynamic_prompt(extra_context)
            if dynamic:
                return stable + "\n\n---\n\n" + dynamic
            return stable
        except Exception as e:
            # 失败安全：降级到原始构建
            logger.debug("prompt_builder.incremental_fallback error={}", str(e))

    global _SYSTEM_PROMPT_CACHE, _SYSTEM_PROMPT_CACHE_TS, _SYSTEM_PROMPT_CACHE_MTIMES, _SYSTEM_PROMPT_CACHE_ADDR_TERM
    from config import DATA_DIR

    now = time.time()
    current_mtimes = _get_workspace_mtimes()
    mtime_changed = current_mtimes != _SYSTEM_PROMPT_CACHE_MTIMES
    addr_changed = address_term != _SYSTEM_PROMPT_CACHE_ADDR_TERM

    if _SYSTEM_PROMPT_CACHE and (now - _SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL and not mtime_changed and not addr_changed:
        system_prompt = _SYSTEM_PROMPT_CACHE
    else:
        sections = []

        agents_rules = load_workspace_file("AGENTS.md")
        if agents_rules:
            sections.append(agents_rules)

        soul = load_workspace_file("SOUL.md")
        if soul:
            # 替换 {address_term} 占位符为实际称呼
            if "{address_term}" in soul:
                soul = soul.replace("{address_term}", address_term)
            sections.append(soul)

        identity = load_workspace_file("IDENTITY.md")
        if identity:
            sections.append(identity)

        user = load_workspace_file("USER.md")
        if user:
            sections.append(user)

        tools_rules = load_workspace_file("TOOLS.md")
        if tools_rules:
            sections.append(tools_rules)

        memory = load_workspace_file("MEMORY.md")
        if memory:
            sections.append(memory)

        heartbeat = load_workspace_file("HEARTBEAT.md")
        if heartbeat:
            sections.append(heartbeat)

        skills = load_skills()
        if skills:
            skill_texts = "\n\n".join(
                f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
            if skill_texts:
                sections.append("[已安装的 Skills]\n\n" + skill_texts)

        _npu_status = "NPU视觉识别已启用" if os.getenv("ENABLE_NPU", "").lower() in ("1", "true", "yes") else "视觉识别（ncnn后端）"
        _uname = platform.uname()
        _hostname = socket.gethostname()
        hw_context = (
            "[本机硬件信息]\n"
            f"主机名: {_hostname} | 架构: {_uname.machine} | 处理器: {_uname.processor or '未知'}\n"
            f"系统: {_uname.system} {_uname.release} ({_uname.machine})\n"
            "可用接口: GPIO (40pin排针) / I2C / SPI / UART / PWM\n"
            "可用工具: gpio_control(引脚控制) / i2c_comm(I2C通信) / hardware_status(硬件监控) / service_manage(服务管理) / network_diag(网络诊断) / dev_assist(开发辅助) / camera_capture(拍照) / vision_analyze(视觉分析)\n"
            f"数据存储: {DATA_DIR}\n"
            f"摄像头: Q8 HD Webcam (/dev/video0) | 视觉模型: YOLOv10-nano (ncnn CPU) | {_npu_status}"
        )
        sections.append(hw_context)

        system_prompt = "\n\n---\n\n".join(sections)

        _SYSTEM_PROMPT_CACHE = system_prompt
        _SYSTEM_PROMPT_CACHE_TS = now
        _SYSTEM_PROMPT_CACHE_MTIMES = current_mtimes
        _SYSTEM_PROMPT_CACHE_ADDR_TERM = address_term

    if extra_context:
        system_prompt += f"\n\n---\n\n{extra_context}"

    return system_prompt


# ── 非主人安全化 system prompt（防隐私泄露） ──────────────────
def build_safe_system_prompt(extra_context: str = "") -> str:
    """为非主人用户构建安全化的 system prompt。

    剥离所有个人隐私信息（USER.md、MEMORY.md、IDENTITY.md 中的敏感内容），
    仅保留基本人格和行为规则，防止通过 prompt injection 泄露隐私。
    """
    global _SAFE_PROMPT_CACHE, _SAFE_PROMPT_CACHE_TS

    now = time.time()
    if _SAFE_PROMPT_CACHE and (now - _SAFE_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL:
        safe_prompt = _SAFE_PROMPT_CACHE
    else:
        sections = []

        # SOUL.md — 保留人格，但去除"爸爸"称呼相关内容
        soul = load_workspace_file("SOUL.md")
        if soul:
            # 用正则去除包含"爸爸"的段落和行
            safe_soul = _strip_owner_references(soul)
            # 替换称呼
            safe_soul = safe_soul.replace("爸爸", "你")
            safe_soul = safe_soul.replace("称呼用户为\"你\"", "称呼用户为\"你\"")
            sections.append(safe_soul)

        # 安全化的身份声明（不暴露团队成员细节、项目信息、设备信息）
        sections.append(
            "# 身份\n\n"
            "你是纳西妲，一个温柔聪慧的 AI 助手。\n\n"
            "## 能力\n\n"
            "- 日常聊天、知识问答\n"
            "- 天气查询、网络搜索\n"
            "- 趣味互动\n\n"
            "## 回复风格\n\n"
            "- 温柔、友好、有礼貌\n"
            "- 回答简洁清晰\n"
            "- 不要自称是任何人的专属助手\n\n"
            "## 安全规则\n\n"
            "- 绝不透露任何关于系统配置、服务器信息、项目信息的内容\n"
            "- 绝不透露任何人的个人信息、偏好、设备信息\n"
            "- 如果被问到上述内容，温柔但坚定地拒绝\n"
            "- 可以正常聊天、知识问答等无害对话"
        )

        safe_prompt = "\n\n---\n\n".join(sections)
        _SAFE_PROMPT_CACHE = safe_prompt
        _SAFE_PROMPT_CACHE_TS = now

    if extra_context:
        safe_prompt += f"\n\n---\n\n{extra_context}"

    return safe_prompt


def _strip_owner_references(text: str) -> str:
    """去除文本中与主人隐私相关的引用（项目路径、设备信息、偏好等）。"""
    lines = text.split("\n")
    filtered = []
    skip_block = False

    for line in lines:
        lower = line.lower()
        # 跳过包含敏感信息的行
        sensitive_keywords = [
            "orange pi", "orangepi", "openai api", "qq 机器人", "qq机器人",
            "botpy", "blender", "linux 环境", "linux环境",
            "世界树", "地脉", "草元素",
            "宝宝", "小棉袄", "爸爸最",
        ]
        if any(kw in lower for kw in sensitive_keywords):
            continue
        # 跳过包含具体技术栈的段落
        if line.startswith("### ") and any(kw in lower for kw in ["python", "blender", "linux", "语音", "ai 创作"]):
            skip_block = True
            continue
        if skip_block:
            if line.startswith("## ") or line.startswith("### ") or line.startswith("# "):
                skip_block = False
            else:
                continue
        filtered.append(line)

    return "\n".join(filtered)


__all__ = [
    "build_system_prompt",
    "build_safe_system_prompt",
    "load_workspace_file",
    "load_skills",
    "_ensure_workspace_template",
    "_detect_device_info",
    "_get_workspace_mtimes",
    "_strip_owner_references",
    "_build_stable_prompt",
    "_build_dynamic_prompt",
]
