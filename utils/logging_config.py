import json
import os
import sys
from pathlib import Path
from loguru import logger
from config import LOG_DIR


def _json_formatter(record):
    """JSON 结构化日志格式，便于日志分析系统采集。

    返回单行 JSON 字串（不含换行），由 sink 负责追加换行。
    """
    subset = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
        "extra": record.get("extra", {}),
    }
    return json.dumps(subset, ensure_ascii=False)


def _json_sink(message: object) -> None:
    """JSON sink：将日志以 JSON 格式输出到 stderr。

    使用 sink 函数而非 format 可调用对象，避免 loguru colorizer
    将 JSON 中的 <...> 误解为颜色标签（如模块名 <string>）。
    """
    sys.stderr.write(_json_formatter(message.record) + "\n")
    sys.stderr.flush()


def _supports_ansi() -> bool:
    if os.environ.get("NO_COLOR", ""):
        return False
    if os.environ.get("FORCE_COLOR", ""):
        return True
    if sys.platform != "win32":
        return sys.stderr.isatty()
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        STD_ERROR_HANDLE = -12
        handle = kernel32.GetStdHandle(STD_ERROR_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
                return True
            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if kernel32.SetConsoleMode(handle, new_mode):
                return True
    except Exception:
        pass
    return False


def setup_logging() -> None:
    """初始化日志系统。

    输出策略：
    - stderr：由环境变量 LOG_FORMAT 控制（json|text，默认 text）
    - 文件 logs/agent_{time}.json：保留原有 loguru serialize 结构化日志
    - 文件 logs/agent.log：新增文本格式日志，便于直接查看

    结构化 extra 字段统一默认值：trace_id / event / duration_ms / user_id /
    session_id / error，调用方可用 logger.bind() 或关键字参数覆盖。
    """
    logger.remove()
    # 统一 extra 字段默认值，便于结构化日志分析
    logger.configure(extra={
        "trace_id": "",
        "event": "",
        "duration_ms": 0,
        "user_id": "",
        "session_id": "",
        "error": "",
    })

    # 通过环境变量切换 stderr 输出格式：json (容器环境) | text (默认，人类可读)
    log_format = os.environ.get("LOG_FORMAT", "text").lower()

    if log_format == "json":
        # JSON 结构化输出到 stderr，便于容器环境收集
        logger.add(
            _json_sink,
            level="INFO",
            backtrace=False,
            diagnose=False,
        )
    else:
        # 默认文本格式（保留原有彩色输出，Windows 不支持 ANSI 时自动关闭）
        _colorize = _supports_ansi()
        logger.add(
            sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[trace_id]}</cyan> | {message}",
            level="DEBUG",
            colorize=_colorize,
        )

    # 确保日志目录存在
    log_dir = LOG_DIR
    log_dir.mkdir(exist_ok=True)

    # 保留原有结构化文件日志（loguru serialize 模式，不破坏现有输出）
    log_path = log_dir / "agent_{time:YYYY-MM-DD}.json"
    logger.add(
        str(log_path),
        format="{time} {level} {extra[trace_id]} {message}",
        serialize=True,
        rotation="00:00",
        retention="30 days",
        level="INFO",
        encoding="utf-8",
        enqueue=True,  # 异步队列写入，避免事件循环阻塞
    )

    # 新增文本格式文件日志 logs/agent.log，便于直接查看
    text_log_path = log_dir / "agent.log"
    logger.add(
        str(text_log_path),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[trace_id]} | {message}",
        rotation="10 MB",
        retention="30 days",
        level="INFO",
        encoding="utf-8",
        enqueue=True,  # 异步队列写入，避免事件循环阻塞
    )

    logger.info("日志系统就绪")
