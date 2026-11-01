"""ابزارهای مشترک: زمان، لاگ، ابزارهای کمکی."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_logger(name: str, level: str = "INFO", log_dir: str = "logs") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s — %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(Path(log_dir) / "assistant.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str, attempts: int = 4, delay: float = 0.1) -> None:
    """نوشتن اتمیک با تلاش مجدد (فایل‌های وضعیت: صف ارسال، معاملات).

    روی ویندوز آنتی‌ویروس/ایندکسر ممکن است موقتاً فایل تازه‌نوشته را قفل کند و
    os.replace خطای PermissionError بدهد؛ چند بار با فاصله‌ی کوتاه تلاش می‌شود.
    در شکست نهایی استثنا بالا می‌آید تا caller فقط هشدار لاگ کند و برنامه نایستد.
    """
    import time

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)