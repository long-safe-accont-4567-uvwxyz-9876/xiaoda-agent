import json
import os
import sys
from loguru import logger
from config import LOG_DIR


# ── 诊断日志 key 列表 ──────────────────────────────────
# 这 4 个模块的诊断日志路由到 logs/diagnostics.log，方便单独排查：
#   1. 上下文压缩 (context.*)     2. 工具循环 (verification.*)
#   3. 工具并发 (tool.*)           4. 技能系统 (tool_registry.* / prompt_builder.*)
#
# WARNING 级别：需要关注的问题（到达上限/压缩回退/工具失败）
# INFO    级别：正常运行信息（压缩触发/循环完成/工具执行/注册统计）
_DIAGNOSTIC_PREFIXES = (
    "context.compress_triggered",
    "context.compressed_with_ccr",
    "context.fallback_compress",
    "context.trim_complete",
    "verification.max_iterations_reached",
    "verification.loop_complete",
    "tool.concurrent_exec_start",
    "tool.concurrent_exec_done",
    "tool.exec_done",
    "tool.exec_failed",
    "tool_registry.builtin_registered",
    "tool_registry.to_openai_tools",
    "prompt_builder.system_prompt_tokens",
)


def _is_diagnostic(record: dict) -> bool:
    """判断日志是否属于诊断日志（按 message 前缀匹配）。"""
    msg = record.get("message", "")
    return any(msg.startswith(prefix) for prefix in _DIAGNOSTIC_PREFIXES)


def _trace_id_patcher(record):
    """loguru patcher：自动将 contextvars 中的 trace_id 注入每条日志。

    无需每处手动 logger.bind(trace_id=...)，中间件层设置一次即可。
    """
    from utils.trace_context import get_trace_id
    tid = get_trace_id()
    if tid:
        record["extra"]["trace_id"] = tid
    return True


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
        logger.debug("logging_config.ansi_check_error", exc_info=True)
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
    # Windows: 确保 stdout/stderr 用 UTF-8，避免 emoji/中文日志在 cp936 控制台崩溃
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    logger.remove()
    # 统一 extra 字段默认值，便于结构化日志分析
    logger.configure(
        patcher=_trace_id_patcher,
        extra={
            "trace_id": "",
            "event": "",
            "duration_ms": 0,
            "user_id": "",
            "session_id": "",
            "error": "",
        },
    )

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

    # 测试模式下跳过文件 sink，防止测试日志污染生产日志
    _is_test_mode = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

    if not _is_test_mode:
        # L6 修复: 文件 sink 创建加 try/except，防止 USB 盘只读时全应用崩溃
        # crash 证据: OSError: [Errno 30] Read-only file system (2026-07-17)
        # CR-FIX: 跟踪已添加的 sink ID，失败时清理部分注册避免残留
        _added_sink_ids = []
        try:
            # 确保日志目录存在
            log_dir = LOG_DIR
            log_dir.mkdir(exist_ok=True)

            # 保留原有结构化文件日志（loguru serialize 模式，不破坏现有输出）
            log_path = log_dir / "agent_{time:YYYY-MM-DD}.json"
            _sid = logger.add(
                str(log_path),
                format="{time} {level} {extra[trace_id]} {message}",
                serialize=True,
                rotation="00:00",
                retention="30 days",
                level="INFO",
                encoding="utf-8",
                enqueue=True,  # 异步队列写入，避免事件循环阻塞
                catch=True,  # 防御：sink 写入异常时不崩溃 logger，避免 Handler 运行时错误导致全应用挂死
            )
            _added_sink_ids.append(_sid)

            # 新增文本格式文件日志 logs/agent.log，便于直接查看
            text_log_path = log_dir / "agent.log"
            _sid = logger.add(
                str(text_log_path),
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[trace_id]} | {message}",
                rotation="10 MB",
                retention="30 days",
                level="INFO",
                encoding="utf-8",
                enqueue=True,  # 异步队列写入，避免事件循环阻塞
                catch=True,  # 防御：sink 写入异常时不崩溃 logger（USB 盘掉线/只读等场景）
            )
            _added_sink_ids.append(_sid)

            # 诊断日志文件：只输出 4 大模块的诊断日志，方便单独排查
            #   - WARNING 级别：max_iterations_reached / fallback_compress / exec_failed
            #   - INFO    级别：compress_triggered / loop_complete / concurrent_exec / to_openai_tools 等
            diag_log_path = log_dir / "diagnostics.log"
            _sid = logger.add(
                str(diag_log_path),
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
                filter=_is_diagnostic,
                rotation="10 MB",
                retention="30 days",
                level="DEBUG",
                encoding="utf-8",
                enqueue=True,
                catch=True,
            )
            _added_sink_ids.append(_sid)
        except (OSError, PermissionError) as e:
            # CR-FIX: 清理已注册的部分 sink，避免 JSON sink 已注册但 text sink 失败时残留
            for _sid in _added_sink_ids:
                try:
                    logger.remove(_sid)
                except (ValueError, KeyError):
                    logger.debug("logging_config.remove_sink_failed", exc_info=True)
            # 日志目录不可写（USB 盘只读/权限不足），降级到 stderr-only
            # 不崩溃应用——stderr sink 已在上面添加，日志仍可输出到控制台
            print(f"[logging] WARNING: 文件日志不可用（{e}），降级到 stderr-only", file=sys.stderr)

    logger.info("日志系统就绪")
