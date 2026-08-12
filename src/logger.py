"""
Structured logging setup.

One rotating log file for the whole service, plus console output (useful
when running interactively during development / debugging on the server).
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import settings

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        # Already configured (e.g. re-imported) - don't add duplicate handlers.
        return logger

    logger.setLevel(settings.log_level)

    formatter = logging.Formatter(_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        _LOG_DIR / "order-sync.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
