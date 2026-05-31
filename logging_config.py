import sys
from pathlib import Path
from loguru import logger
from config import LOG_DIR


def setup_logging():
    logger.remove()
    logger.configure(extra={"trace_id": ""})

    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[trace_id]}</cyan> | {message}",
        level="DEBUG",
    )

    log_dir = LOG_DIR
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "agent_{time:YYYY-MM-DD}.json"
    logger.add(
        str(log_path),
        format="{time} {level} {extra[trace_id]} {message}",
        serialize=True,
        rotation="00:00",
        retention="30 days",
        level="INFO",
        encoding="utf-8",
    )

    logger.info("日志系统就绪")
