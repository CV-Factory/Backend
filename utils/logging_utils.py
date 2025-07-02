from __future__ import annotations

import logging
import os
import sys

__all__ = ["configure_logging"]

class _MaxLevelFilter(logging.Filter):
    """Filter that only passes records with level BELOW max_level."""

    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        return record.levelno < self.max_level


def configure_logging() -> None:
    """Configure root logger so that:

    * INFO/DEBUG 등은 **stdout**
    * ERROR 이상은 **stderr**

    환경 변수 ``LOG_LEVEL``(기본 INFO) 로 전체 최소 레벨을 조정한다.
    여러 모듈에서 반복 호출되어도 안전하도록 기존 핸들러를 제거한 뒤 재설정한다.
    """
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    root = logging.getLogger()
    # 기존 핸들러 제거
    for h in root.handlers[:]:
        root.removeHandler(h)

    root.setLevel(log_level)
    _fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(_fmt)

    # stdout handler (아래 ERROR)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.addFilter(_MaxLevelFilter(logging.ERROR))
    stdout_handler.setFormatter(formatter)

    # stderr handler (ERROR 이상)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)

    # Prevent duplicate log entries from Celery / Uvicorn child loggers
    for noisy_logger_name in [
        "celery",  # Celery worker internal logger
        "celery.worker",  # more granular celery loggers
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ]:
        logging.getLogger(noisy_logger_name).propagate = False 