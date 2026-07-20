"""Enhanced logging with debug flags, persistent logs, and structured output."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config


class EnhancedLogger:
    """Thread-safe logger with debug levels, file persistence, and structured output."""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        debug: bool = False,
        persist: bool = True,
        max_lines: int = 10000,
    ):
        self.debug = debug
        self.persist = persist
        self.max_lines = max_lines
        self.log_file: Optional[Path] = None
        self._file_handler: Optional[logging.FileHandler] = None
        self._console_handler: Optional[logging.StreamHandler] = None
        self._logger = logging.getLogger("openvidia")
        self._logger.setLevel(logging.DEBUG if debug else logging.INFO)
        self._logger.handlers = []

        # Prevent duplicate handlers
        if not self._logger.handlers:
            # Console handler
            console_fmt = logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%H:%M:%S",
            )
            self._console_handler = logging.StreamHandler(sys.stdout)
            self._console_handler.setFormatter(console_fmt)
            self._console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
            self._logger.addHandler(self._console_handler)

            # File handler (optional persistence)
            if persist:
                log_dir = log_dir or config.config_dir() / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.log_file = log_dir / f"openvidia_{timestamp}.log"
                self._file_handler = logging.FileHandler(self.log_file)
                self._file_handler.setFormatter(console_fmt)
                self._file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
                self._logger.addHandler(self._file_handler)
                self._logger.info(f"Log file: {self.log_file}")

        # Rotating buffer for in-memory access (dashboard, SSE)
        self._buffer: list[str] = []

    def _add_to_buffer(self, msg: str) -> None:
        self._buffer.append(msg)
        if len(self._buffer) > self.max_lines:
            self._buffer.pop(0)

    def _format_msg(self, level: str, msg: str, **kwargs) -> str:
        """Format message with optional structured data."""
        if kwargs:
            import json
            extra = json.dumps(kwargs, default=str)
            return f"{msg} | {extra}"
        return msg

    def info(self, msg: str, **kwargs) -> None:
        formatted = self._format_msg("INFO", msg, **kwargs)
        self._logger.info(formatted)
        self._add_to_buffer(formatted)

    def debug(self, msg: str, **kwargs) -> None:
        if self.debug:
            formatted = self._format_msg("DEBUG", msg, **kwargs)
            self._logger.debug(formatted)
            self._add_to_buffer(formatted)

    def warning(self, msg: str, **kwargs) -> None:
        formatted = self._format_msg("WARNING", msg, **kwargs)
        self._logger.warning(formatted)
        self._add_to_buffer(formatted)

    def error(self, msg: str, **kwargs) -> None:
        formatted = self._format_msg("ERROR", msg, **kwargs)
        self._logger.error(formatted)
        self._add_to_buffer(formatted)

    def critical(self, msg: str, **kwargs) -> None:
        formatted = self._format_msg("CRITICAL", msg, **kwargs)
        self._logger.critical(formatted)
        self._add_to_buffer(formatted)

    def get_recent_logs(self, n: int = 100) -> list[str]:
        """Return last N log lines for dashboard/SSE."""
        return self._buffer[-n:]

    def clear_buffer(self) -> None:
        self._buffer.clear()


# Global logger instance (lazy init)
_logger: Optional[EnhancedLogger] = None


def get_logger(debug: bool = False, persist: bool = True) -> EnhancedLogger:
    """Get or create the global logger instance."""
    global _logger
    if _logger is None:
        _logger = EnhancedLogger(debug=debug, persist=persist)
    return _logger


def set_logger(logger: EnhancedLogger) -> None:
    """Set a custom logger instance."""
    global _logger
    _logger = logger
