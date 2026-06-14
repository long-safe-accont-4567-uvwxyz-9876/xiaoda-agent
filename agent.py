import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="纳西妲 AI Agent")
    parser.add_argument("--web", action="store_true", help="启动 Web UI 模式")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEBUI_PORT", "8080")), help="Web UI 端口")
    parser.add_argument("--host", type=str, default=os.getenv("WEBUI_HOST", "0.0.0.0"), help="Web UI 监听地址")
    parser.add_argument("--setup", action="store_true", help="运行配置向导")
    args = parser.parse_args()

    # 首次启动自动触发配置向导
    if args.setup:
        from setup_wizard import main as wizard_main
        wizard_main()
        return

    from setup_wizard import is_first_run
    if is_first_run():
        print("\n  \033[93m⚠ 检测到首次运行，启动配置向导...\033[0m\n")
        from setup_wizard import main as wizard_main
        wizard_main()
        # 向导完成后重新加载 .env
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
    logger.info("agent.web.start", host=host, port=port)

    uvicorn.run(
        "web.server:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
