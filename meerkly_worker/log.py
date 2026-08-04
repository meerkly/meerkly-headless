"""Structured logging: one JSON object per line on stdout.

Container-native — the runtime collects stdout, so there is no file rotation or
retention here. Never log page HTML or a token.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

PRIORITY = {"debug": 0, "info": 1, "warn": 2, "error": 3}


class Logger:
    def __init__(self, level: str) -> None:
        # An unknown level must not pass everything through.
        self._threshold = PRIORITY.get(level, PRIORITY["info"])

    def debug(self, message: str, **fields) -> None:
        self._emit("debug", message, fields)

    def info(self, message: str, **fields) -> None:
        self._emit("info", message, fields)

    def warn(self, message: str, **fields) -> None:
        self._emit("warn", message, fields)

    def error(self, message: str, **fields) -> None:
        self._emit("error", message, fields)

    def _emit(self, level: str, message: str, fields: dict) -> None:
        if PRIORITY[level] < self._threshold:
            return
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "message": message,
        }
        if fields:
            entry["data"] = fields
        stream = sys.stderr if level in ("warn", "error") else sys.stdout
        print(json.dumps(entry, default=str), file=stream, flush=True)


def get_logger(level: str) -> Logger:
    return Logger(level)
