"""Structured event logger — persists all [TAG] events to daily JSONL files.

Every significant system event (entry, exit, fill, risk, reconciliation, etc.)
is captured as a JSON line for post-trade analysis and ML training.

Files: logs/structured/YYYY-MM-DD.jsonl
Retention: managed externally (recommend 90 days).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STRUCTURED_LOG_DIR = Path("logs/structured")

# Singleton instance — import and use directly
_instance: "StructuredLogger | None" = None


def get_structured_logger() -> "StructuredLogger":
    """Get or create the singleton StructuredLogger."""
    global _instance
    if _instance is None:
        _instance = StructuredLogger()
    return _instance


class StructuredLogger:
    """Append-only JSONL logger for structured trading events.

    Usage:
        from src.core.structured_logger import get_structured_logger
        slog = get_structured_logger()
        slog.log("ENTRY", strategy_id="nifty_1", leg="PREMIUM", mode="strangle", ...)
        slog.log("FILL", order_id="abc", symbol="NIFTY25MAR22000CE", fill_price=150.0, ...)
    """

    def __init__(self, output_dir: Path = STRUCTURED_LOG_DIR):
        self._dir = output_dir
        self._current_date: str = ""
        self._file = None

    def log(self, tag: str, **fields) -> None:
        """Write one structured event to today's JSONL file.

        Args:
            tag: Event type (e.g., "ENTRY", "EXIT", "FILL", "RISK_PROXIMITY").
            **fields: Arbitrary key-value pairs for the event.
        """
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            self._ensure_file(today)

            record = {
                "timestamp": now.isoformat(timespec="milliseconds"),
                "tag": tag,
                **fields,
            }
            self._file.write(json.dumps(record, default=str) + "\n")
            self._file.flush()
        except Exception:
            logger.exception(f"[STRUCTURED_LOG] Failed to write {tag} event")

    def close(self) -> None:
        """Close the current file handle."""
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def _ensure_file(self, date_str: str) -> None:
        """Open or rotate the JSONL file for today."""
        if self._current_date == date_str and self._file:
            return

        self.close()
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{date_str}.jsonl"
        self._file = open(path, "a")
        self._current_date = date_str
