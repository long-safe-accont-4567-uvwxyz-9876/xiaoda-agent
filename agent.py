from typing import Any
import os
import sys
import asyncio
import argparse
from pathlib import Path

from loguru import logger
import contextlib

def _safe_int(val, default):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    # PyInstaller frozen 模式下使用用户目录（~/.ai-agent/.env），
    # 因为安装到 C:\Program Files\ 时非管理员用户无法写入 .env
    if getattr(sys, 'frozen', False):
        _env_dir = Path.home() / ".ai-agent"
        _env_dir.mkdir(parents=True, exist_ok=True)
        _env_path = str(_env_dir / ".env")
    else:
        _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path, override=True)
except Exception:
    # dotenv 加载失败时写日志，防止 exe 静默崩溃
    import traceback
    import pathlib
    try:
        log_dir = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "crash.log").write_text(
            f"Failed to load dotenv:\n{traceback.format_exc()}", encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        logger.debug("dotenv.load_failed", exc_info=True)
    raise


def _setup_windows_event_loop() -> None:
    """Windows: 使用 SelectorEventLoop 加速 aiosqlite 线程切换。

    ProactorEventLoop 做 aiosqlite 线程间通知比 Linux 慢 3-5 倍，
    改用 WindowsSelectorEventLoopPolicy 消除线程切换延迟。
    非 Windows 平台不做任何改动，沿用平台默认行为。
    必须在任何 asyncio 事件循环创建之前调用（早于 uvicorn / aiosqlite）。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    # Windows: 使用 SelectorEventLoop 加速 aiosqlite 线程切换（ProactorEventLoop 慢 3-5 倍）
    # 必须早于任何 asyncio/uvicorn 调用，确保 _run_web/_run_desktop/_run_cli 三路径均生效
    _setup_windows_event_loop()

    parser = argparse.ArgumentParser(description="Nahida AI Agent")
    subparsers = parser.add_subparsers(dest="command")

    # doctor 子命令: xiaoda-agent doctor [--json] [--fix]
    doctor_parser = subparsers.add_parser("doctor", help="运行自检 (零 API 调用, <2s)")
    doctor_parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    doctor_parser.add_argument("--fix", action="store_true", help="自动修复可修复的问题")

    # 默认模式参数
    parser.add_argument("--web", action="store_true", help="启动 Web UI 模式")
    parser.add_argument("--desktop", action="store_true", help="启动桌面模式（pywebview 原生窗口）")
    parser.add_argument("--port", type=int, default=_safe_int(os.getenv("WEBUI_PORT", "8082"), 8082), help="Web UI 端口")
    parser.add_argument("--host", type=str, default=os.getenv("WEBUI_HOST", "127.0.0.1"), help="Web UI 监听地址")
    parser.add_argument("--setup", action="store_true", help="运行配置向导")
    args = parser.parse_args()

    # doctor 子命令: 零 API 调用自检, <2s 完成
    if args.command == "doctor":
        from core.doctor import run_doctor
        sys.exit(run_doctor(json_output=args.json, auto_fix=args.fix))

    # 首次启动自动触发配置向导
    if args.setup:
        from setup_wizard import main as wizard_main
        wizard_main()
        return

    from setup_wizard import is_first_run, ENV_PATH, ENV_EXAMPLE_PATH
    if is_first_run():
        # 确保 .env 文件存在（从 .env.example 复制），这样 WebUI Setup 页面能读取默认值
        if not os.path.exists(ENV_PATH):
            import shutil
            if os.path.exists(ENV_EXAMPLE_PATH):
                shutil.copy2(ENV_EXAMPLE_PATH, ENV_PATH)
                print("  [i] 已从 .env.example 创建 .env 配置文件")
            else:
                import tempfile
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(ENV_PATH), prefix=".env.tmp")
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        f.write("")
                    os.replace(tmp_path, ENV_PATH)
                except (OSError, PermissionError):
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)
                    raise
                print("  [i] 已创建空 .env 配置文件")
            # 重新加载 .env 使默认值生效
            load_dotenv(ENV_PATH, override=True)

        if args.web:
            # Web 模式下不弹出 CLI 向导，由 WebUI /setup 页面引导配置
            print("\n  [!] 检测到首次运行，将以降级模式启动 WebUI")
            print("      请在浏览器中打开 WebUI 完成 API Key 配置\n")
        else:
            print("\n  [!] 检测到首次运行，启动配置向导...\n")
            from setup_wizard import main as wizard_main
            wizard_main()
            # 向导完成后重新加载 .env
            load_dotenv(ENV_PATH, override=True)

    if args.desktop:
        _run_desktop(args.host, args.port)
    elif args.web or os.getenv("WEB_UI_ENABLED", "").lower() in ("true", "1", "yes"):
        _run_web(args.host, args.port)
    else:
        _run_cli()


def _run_cli() -> None:
    from cli import CLIInterface
    cli = CLIInterface()
    cli.run()


def _is_running_in_docker() -> bool:
    """检测当前是否在 Docker 容器内运行。"""
    import os
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", errors="ignore") as f:
            return "docker" in f.read()
    except OSError:
        return False


def _get_lan_addresses() -> list:
    """检测本机主网卡的局域网 IPv4 地址（纯本地枚举，无网络请求）。"""
    import socket
    try:
        # 使用本地接口枚举，避免向外部IP发送探测包
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ips = [a[4][0] for a in addrs if not a[4][0].startswith("127.")]
        return ips[:1] if ips else []
    except (OSError, socket.gaierror):
        logger.debug("agent.lan_address_detect_failed", exc_info=True)
    return []


def _run_web(host: str, port: int) -> None:
    import uvicorn
    from utils.logging_config import setup_logging
    setup_logging()

    from loguru import logger
    logger.info("agent.web.start", port=port)

    # 端口冲突检测（异步版，避免主线程 time.sleep 阻塞）
    asyncio.run(_wait_for_port_available_async(host, port))

    # 直接传 app 对象，避免 uvicorn 动态导入失败（PyInstaller 兼容）
    try:
        from web.server import app
    except Exception:
        import traceback
        import pathlib
        log_path = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent" / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"Failed to import web.server:\n{traceback.format_exc()}", encoding="utf-8")
        raise

    # 显示友好的访问地址（0.0.0.0 对用户不友好）
    display_host = "localhost" if host == "0.0.0.0" else host
    logger.info(f"Web UI: http://{display_host}:{port}")

    # 检测局域网 IP，打印手机可访问的地址
    if host == "0.0.0.0":
        if _is_running_in_docker():
            # Docker 容器内检测到的是容器 IP，对用户无用
            # 提示用户用宿主机 IP + 映射端口访问
            logger.info("Docker 模式: 请使用宿主机 IP 访问（端口映射见 docker run -p 参数）")
        else:
            lan_ips = _get_lan_addresses()
            if lan_ips:
                logger.info("手机访问（同一 WiFi 下）:")
                for ip in lan_ips:
                    logger.info(f"  http://{ip}:{port}")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


async def _wait_for_port_available_async(host: str, port: int) -> None:
    """端口冲突检测（异步版）：等待旧进程释放端口，最多 60s。

    用 asyncio.sleep 替代 time.sleep，避免阻塞事件循环。
    """
    import socket
    from loguru import logger
    for attempt in range(30):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.settimeout(1)
                s.bind((host, port))
                break
        except OSError:
            if attempt == 0:
                logger.warning(f"agent.port_in_use port={port}, waiting for old process to release...")
            if attempt < 29:
                await asyncio.sleep(2)
            else:
                logger.error(f"agent.port_still_in_use port={port}, giving up after 60s")
                sys.exit(1)


def _wait_for_port_available(host: str, port: int) -> None:
    """端口冲突检测（桌面模式用，同步）：等待旧进程释放端口，最多 60s。

    桌面模式此时 UI 尚未启动，主线程同步 sleep 仅影响 splash 显示时长，可接受。
    重试间隔缩短到 0.5s 以减少 splash 等待。
    """
    import socket
    import time
    from loguru import logger
    for attempt in range(120):  # 120 * 0.5s = 60s
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.settimeout(1)
                s.bind((host, port))
                break
        except OSError:
            if attempt == 0:
                logger.warning(f"agent.port_in_use port={port}, waiting for old process to release...")
            if attempt < 119:
                time.sleep(0.5)
            else:
                logger.error(f"agent.port_still_in_use port={port}, giving up after 60s")
                sys.exit(1)


def _import_web_server_safe() -> Any:
    """导入 web.server，失败时写入 crash.log 后重新抛出。"""
    try:
        from web.server import app
        return app
    except (ImportError, SyntaxError, ModuleNotFoundError):
        import traceback
        import pathlib
        log_path = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent" / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"Failed to import web.server:\n{traceback.format_exc()}", encoding="utf-8")
        raise


def _start_splash_server(port: int) -> str:
    """启动独立 HTTP 服务器提供 splash 页面，返回 splash_url。

    端口被占用时回退到 file:// 协议。
    """
    import threading
    import http.server
    import functools
    from loguru import logger

    def _splash_dir() -> Any:
        if getattr(sys, 'frozen', False):
            _base = os.path.dirname(sys.executable)
            for p in [os.path.join(_base, '_internal', 'web', 'splash'),
                      os.path.join(_base, 'web', 'splash')]:
                if os.path.exists(p):
                    return p
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'splash')

    _splash_port = 18089
    _handler_cls = functools.partial(http.server.SimpleHTTPRequestHandler, directory=_splash_dir())
    try:
        _splash_httpd = http.server.HTTPServer(("127.0.0.1", _splash_port), _handler_cls)
        threading.Thread(target=_splash_httpd.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{_splash_port}/splash.html#{port}'
    except OSError:
        logger.warning(f"Splash HTTP 端口 {_splash_port} 被占用, 回退到 file://")
        return 'file://' + os.path.join(_splash_dir(), 'splash.html') + '#' + str(port)


def _wait_for_server_ready(window: Any, port: int) -> None:
    """后台线程：等待 WebUI 就绪后调用 splash.js 的 onServerReady。"""
    import time
    import urllib.request
    from loguru import logger

    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/", timeout=2)
            break
        except (urllib.error.URLError, OSError, ConnectionError):
            time.sleep(1)
    else:
        window.evaluate_js("if(typeof onServerTimeout==='function')onServerTimeout();")
        return

    # WebUI 就绪，等待 splash 页面加载完成后调用 onServerReady
    time.sleep(1.5)
    for attempt in range(5):
        try:
            result = window.evaluate_js(
                "typeof onServerReady==='function' ? (onServerReady(), 'ok') : 'wait'"
            )
            if result and 'ok' in str(result):
                logger.info("splash.onServerReady() triggered")
                return
        except Exception as e:
            logger.warning(f"evaluate_js attempt {attempt}: {e}")
        time.sleep(1)
    logger.warning("splash.onServerReady() failed after retries")


def _run_desktop(host: str, port: int) -> None:
    """桌面模式：pywebview 包装 WebUI，带启动动画"""
    # 控制台已在文件顶部隐藏，此处无需重复
    import threading
    from utils.logging_config import setup_logging
    setup_logging()

    from loguru import logger
    logger.info("agent.desktop.start", port=port)

    # 1. 端口冲突检测
    _wait_for_port_available(host, port)

    # 2. 导入 web.server
    app = _import_web_server_safe()

    # 3. 后台线程启动 uvicorn
    import uvicorn
    server_config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(server_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # 4. 启动 splash 独立 HTTP 服务器
    splash_url = _start_splash_server(port)
    webui_url = f"http://localhost:{port}"
    logger.info(f"Desktop splash: {splash_url}")
    logger.info(f"Desktop WebUI: {webui_url}")

    # 5. 创建 pywebview 窗口
    import webview
    window = webview.create_window(
        title="Xiaoda Agent",
        url=splash_url,
        width=1280,
        height=800,
        min_size=(960, 600),
        text_select=False,
    )

    # 6. 后台线程：等待服务就绪后通知 splash.js 显示进入按钮
    checker_thread = threading.Thread(
        target=_wait_for_server_ready, args=(window, port), daemon=True
    )
    checker_thread.start()

    # 7. WebView2 合成器激活：页面加载后强制触发 reflow，
    #    修复 Windows 上动画/JS 更新不渲染直到用户按键的问题。
    _reflow_js = (
        "(function(){"
        "  var b=document.body;"
        "  void b.offsetHeight;"  # 触发一次同步 reflow
        "  b.style.opacity='0.999';"
        "  requestAnimationFrame(function(){b.style.opacity='1';});"
        "  // 持续推进 rAF，防止合成器再次休眠（直到 onServerReady 接管）"
        "  var t0=performance.now();"
        "  (function kick(){if(performance.now()-t0<30000)requestAnimationFrame(kick);})();"
        "  return 'ok';"
        "})()"
    )
    def _on_loaded():
        try:
            window.evaluate_js(_reflow_js)
        except (RuntimeError, OSError):
            logger.debug("pywebview.reflow_js_failed", exc_info=True)
        # 不再在 UI 线程轮询；reflow 兜底由 JS 端 setInterval 自驱（见 splash.js _reflowKicker）
        # _on_loaded 在 1 秒内返回，避免阻塞 pywebview UI 线程导致桌面模式冻死

    window.events.loaded += _on_loaded

    # 8. 启动 pywebview（主线程阻塞）
    webview.start(debug=False)

    # 窗口关闭后退出进程
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 顶层异常兜底：写日志文件，防止 exe 静默崩溃
        import traceback
        import pathlib
        try:
            log_dir = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "crash.log").write_text(
                f"xiaoda-agent crash:\n{traceback.format_exc()}", encoding="utf-8"
            )
        except (OSError, PermissionError):
            logger.debug("crash.log.write_failed", exc_info=True)
        # 同时输出到 stderr（如果终端可见的话）
        traceback.print_exc()
        sys.exit(1)
