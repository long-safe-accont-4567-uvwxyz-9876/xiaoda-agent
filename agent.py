import os
import sys
import argparse
from pathlib import Path

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
except Exception as e:
    # dotenv 加载失败时写日志，防止 exe 静默崩溃
    import traceback, pathlib
    try:
        log_dir = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "crash.log").write_text(
            f"Failed to load dotenv:\n{traceback.format_exc()}", encoding="utf-8"
        )
    except Exception:
        pass
    raise


def main():
    parser = argparse.ArgumentParser(description="Nahida AI Agent")
    parser.add_argument("--web", action="store_true", help="启动 Web UI 模式")
    parser.add_argument("--desktop", action="store_true", help="启动桌面模式（pywebview 原生窗口）")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEBUI_PORT", "8082")), help="Web UI 端口")
    parser.add_argument("--host", type=str, default=os.getenv("WEBUI_HOST", "0.0.0.0"), help="Web UI 监听地址")
    parser.add_argument("--setup", action="store_true", help="运行配置向导")
    args = parser.parse_args()

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
                print(f"  [i] 已从 .env.example 创建 .env 配置文件")
            else:
                with open(ENV_PATH, "w", encoding="utf-8") as f:
                    f.write("")
                print(f"  [i] 已创建空 .env 配置文件")
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


def _run_cli():
    from cli import CLIInterface
    cli = CLIInterface()
    cli.run()


def _get_lan_addresses():
    """检测本机主网卡的局域网 IPv4 地址（不产生实际网络流量）。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if primary_ip and not primary_ip.startswith("127."):
            return [primary_ip]
    except Exception:
        pass
    return []


def _run_web(host: str, port: int):
    import socket
    import uvicorn
    from utils.logging_config import setup_logging
    setup_logging()

    from loguru import logger
    logger.info("agent.web.start", port=port)

    # 端口冲突检测：启动前检查端口是否可用，等待旧进程释放
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
                import time
                time.sleep(2)
            else:
                logger.error(f"agent.port_still_in_use port={port}, giving up after 60s")
                sys.exit(1)

    # 直接传 app 对象，避免 uvicorn 动态导入失败（PyInstaller 兼容）
    try:
        from web.server import app
    except Exception as e:
        import traceback, pathlib
        log_path = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent" / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"Failed to import web.server:\n{traceback.format_exc()}", encoding="utf-8")
        raise

    # 显示友好的访问地址（0.0.0.0 对用户不友好）
    display_host = "localhost" if host == "0.0.0.0" else host
    logger.info(f"Web UI: http://{display_host}:{port}")

    # 检测局域网 IP，打印手机可访问的地址
    if host == "0.0.0.0":
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


def _run_desktop(host: str, port: int):
    """桌面模式：pywebview 包装 WebUI，带启动动画"""
    # 控制台已在文件顶部隐藏，此处无需重复

    import threading
    import socket
    import time
    import urllib.request
    from utils.logging_config import setup_logging
    setup_logging()

    from loguru import logger
    logger.info("agent.desktop.start", port=port)

    # 端口冲突检测（复用 _run_web 的逻辑）
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
                time.sleep(2)
            else:
                logger.error(f"agent.port_still_in_use port={port}, giving up after 60s")
                sys.exit(1)

    # 导入 web.server
    try:
        from web.server import app
    except Exception as e:
        import traceback, pathlib
        log_path = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent" / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"Failed to import web.server:\n{traceback.format_exc()}", encoding="utf-8")
        raise

    # 后台线程启动 uvicorn
    import uvicorn
    server_config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(server_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # 确定 splash screen 路径
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
        splash_dir = os.path.join(base_dir, '_internal', 'web', 'splash')
        if not os.path.isdir(splash_dir):
            splash_dir = os.path.join(base_dir, 'web', 'splash')
    else:
        splash_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'splash')

    splash_path = os.path.join(splash_dir, 'splash.html')
    splash_url = f"file://{splash_path}"
    webui_url = f"http://localhost:{port}"

    logger.info(f"Desktop splash: {splash_url}")
    logger.info(f"Desktop WebUI: {webui_url}")

    # pywebview JS API
    class DesktopAPI:
        def enter_world(self):
            """用户点击'进入提瓦特大陆'按钮后，切换到 WebUI"""
            import webview
            for w in webview.windows:
                w.load_url(webui_url)
            return True

    # 创建 pywebview 窗口
    import webview
    api = DesktopAPI()
    window = webview.create_window(
        title="Nahida Agent",
        url=splash_url,
        width=1280,
        height=800,
        min_size=(960, 600),
        text_select=False,
        js_api=api,
    )

    # 后台线程：等待服务就绪后通知 splash.js 显示进入按钮
    def wait_for_server():
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://localhost:{port}/", timeout=2)
                window.evaluate_js("if(typeof onServerReady==='function')onServerReady();")
                return
            except Exception:
                time.sleep(1)
        window.evaluate_js("if(typeof onServerTimeout==='function')onServerTimeout();")

    checker_thread = threading.Thread(target=wait_for_server, daemon=True)
    checker_thread.start()

    # 启动 pywebview（主线程阻塞）
    webview.start(debug=False)

    # 窗口关闭后退出进程
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 顶层异常兜底：写日志文件，防止 exe 静默崩溃
        import traceback, pathlib
        try:
            log_dir = pathlib.Path(os.environ.get("APPDATA", ".")) / "xiaoda-agent"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "crash.log").write_text(
                f"xiaoda-agent crash:\n{traceback.format_exc()}", encoding="utf-8"
            )
        except Exception:
            pass
        # 同时输出到 stderr（如果终端可见的话）
        traceback.print_exc()
        sys.exit(1)
