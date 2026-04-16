"""Structured logging setup using structlog."""

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "trading.log"

# 50 MB per file, keep last 10 — ~500 MB max, covers ~weeks of trading logs
_MAX_BYTES = 50 * 1024 * 1024
_BACKUP_COUNT = 10


def setup_logging(log_level: str = "INFO", json_output: bool = False) -> None:
    """Configure structured logging for the application.

    Writes to both stdout (console) and logs/trading.log (rotating file).
    The file always uses plain-text format for easy grep/tail regardless of
    json_output — JSON is only for stdout in production mode.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, etc.)
        json_output: Use JSON output on stdout (production) vs console (development).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # ── structlog (stdout) ────────────────────────────────────────────────────
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ── stdlib root logger ────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)

    # Console handler (stdout)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    # Rotating file handler — always plain text, easy to grep
    LOG_DIR.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    # ── Silence noisy third-party loggers ─────────────────────────────────────
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
