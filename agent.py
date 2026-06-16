import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception as e:
    import traceback, pathlib
    try:
        log_dir = pathlib.Path(os.environ.get("APPDATA", ".")) / "nahida-agent"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "crash.log").write_text(
            f"Failed to load dotenv:\n{traceback.format_exc()}", encoding="utf-8"
        )
    except Exception:
        pass
    raise


def main():
    parser = argparse.ArgumentParser(description="纳西妲 AI Agent")
    parser.add_argument("--web", action="store_true", help="启动 Web UI 模式")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEBUI_PORT", "8082")), help="Web UI 端口")
    parser.add_argument("--host", type=str, default=os.getenv("WEBUI_HOST", "0.0.0.0"), help="Web UI 监听地址")
    parser.add_argument("--setup", action="store_true", help="运行配置向导")
    args = parser.parse_args()

    if args.setup:
        from setup_wizard import main as wizard_main
        wizard_main()
        return

    from setup_wizard import is_first_run
    if is_first_run():
        if args.web:
            print("\n  [!] 检测到首次运行，将以降级模式启动 WebUI")
            print("      请在浏览器中打开 WebUI 完成 API Key 配置\n")
        else:
            print("\n  [!] 检测到首次运行，启动配置向导...\n")
            from setup_wizard import main as wizard_main
            wizard_main()
            load_dotenv(override=True)

    if args.web or os.getenv("WEB_UI_ENABLED", "").lower() in ("true", "1", "yes"):
        _run_web(args.host, args.port)
    else:
        _run_cli()


def _run_cli():
    from cli import CLIInterface
    cli = CLIInterface()
    cli.run()


def _run_web(host: str, port: int):
    import uvicorn
    from utils.logging_config import setup_logging
    setup_logging()

    from loguru import logger
    logger.info("agent.web.start", port=port)
    print(f"[DEBUG] WEBUI_PORT env={os.getenv('WEBUI_PORT', 'NOT SET')} using_port={port}", flush=True)

    try:
        from web.server import app
    except Exception as e:
        import traceback, pathlib
        log_path = pathlib.Path(os.environ.get("APPDATA", ".")) / "nahida-agent" / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"Failed to import web.server:\n{traceback.format_exc()}", encoding="utf-8")
        raise

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback, pathlib
        try:
            log_dir = pathlib.Path(os.environ.get("APPDATA", ".")) / "nahida-agent"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "crash.log").write_text(
                f"nahida-agent crash:\n{traceback.format_exc()}", encoding="utf-8"
            )
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
