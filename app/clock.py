"""ساعت قابل‌جایگزینی؛ در حالت Replay زمان ضبط‌شده جایگزین زمان واقعی می‌شود."""
from __future__ import annotations

import time
from datetime import datetime

_fixed: float | None = None


def now() -> float:
    return _fixed if _fixed is not None else time.time()


def set_time(ts: float | None) -> None:
    global _fixed
    _fixed = ts


def hhmmss(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else now()).strftime("%H:%M:%S")
