import os
import sys
import logging
from loguru import logger


def setup_logging(level: str = "INFO", log_dir: str = "logs"):
    os.makedirs(log_dir, exist_ok=True)

    logger.remove()

    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )

    logger.add(
        os.path.join(log_dir, "nahida_{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        rotation="1 day",
        retention="7 days",
        encoding="utf-8",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
