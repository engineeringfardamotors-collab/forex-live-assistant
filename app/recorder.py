"""ضبط کامل جلسه برای بازبینی و Replay (بخش ۱۰).

ساختار:  session_data/sessions/<YYYYmmdd_HHMMSS>/
    events.jsonl   هر خط یک رویداد با زمان t (epoch):
                   transcript {text, t0, t1, audio}
                   frame      {file, reason, info}
                   analysis   {frame, request, response, prompt_version, latency}
                   decision   {intent, result, signal, issues}
                   market     {symbol, price, src}
                   meta / note
    frames/        فریم‌هایی که به مدل داده شدند
    audio/         قطعه‌های صوتی (اگر keep_audio)
    labels.json    برچسب‌های دستی سیگنال‌های واقعی (برای سنجش دقت)
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app import clock
from app.utils import get_logger


class SessionRecorder:
    def __init__(self, cfg: dict[str, Any], root: Path | None = None):
        r = cfg.get("recording", {})
        self.enabled = bool(r.get("enabled", True))
        self.keep_audio = bool(r.get("keep_audio", True))
        self.keep_days = float(r.get("keep_days", 14) or 0)          # 0 = حذف خودکار خاموش
        self.compress_after_days = float(r.get("compress_after_days", 3) or 0)  # 0 = فشرده‌سازی خاموش
        self.root = root or Path("session_data/sessions")
        self.dir: Path | None = None
        self._lock = threading.Lock()
        self._log = get_logger("recorder", cfg.get("logging", {}).get("level", "INFO"),
                               cfg.get("logging", {}).get("dir", "logs"))

    def start(self, meta: dict[str, Any]) -> Path | None:
        if not self.enabled:
            return None
        self.dir = self.root / datetime.now().strftime("%Y%m%d_%H%M%S")
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        (self.dir / "audio").mkdir(parents=True, exist_ok=True)
        self.write("meta", started=time.time(), **meta)
        self._rotate()
        return self.dir

    def stop(self) -> None:
        if self.dir:
            self.write("meta", ended=time.time())
        self.dir = None

    # ---------- data rotation (بخش ۲۲-۱۱) ----------
    def _session_age_days(self, d: Path) -> float:
        try:
            ts = datetime.strptime(d.name[:15], "%Y%m%d_%H%M%S")
            return (time.time() - ts.timestamp()) / 86400
        except ValueError:
            return (time.time() - d.stat().st_mtime) / 86400

    def _rotate(self) -> None:
        """جلسه‌های قدیمی: فشرده‌سازی به zip پس از compress_after_days، حذف پس از keep_days."""
        if self.keep_days <= 0 or not self.root.exists():
            return
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or len(d.name) < 8 or not d.name[:8].isdigit():
                continue
            try:
                age = self._session_age_days(d)
                zip_path = d.with_name(d.name + ".zip")
                if age > self.keep_days:
                    shutil.rmtree(d, ignore_errors=True)
                    self._log.info("جلسه‌ی قدیمی حذف شد: %s (%.0f روز)", d.name, age)
                elif 0 < self.compress_after_days < age and not zip_path.exists():
                    shutil.make_archive(str(d), "zip", root_dir=d)
                    shutil.rmtree(d, ignore_errors=True)
                    self._log.info("جلسه‌ی قدیمی فشرده شد: %s.zip (%.0f روز)", d.name, age)
            except Exception as e:  # noqa: BLE001
                self._log.warning("چرخش داده برای %s ناموفق بود: %s", d.name, e)

    def write(self, kind: str, **data: Any) -> None:
        if not self.dir:
            return
        rec = {"t": clock.now(), "type": kind, **data}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock, open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def save_frame(self, img, reason: str) -> Path | None:
        if not self.dir:
            return None
        name = f"{datetime.fromtimestamp(clock.now()).strftime('%H%M%S_%f')[:-3]}_{reason}.png"
        path = self.dir / "frames" / name
        img.save(path, "PNG")
        return path

    def keep_audio_file(self, wav: Path) -> str | None:
        if not self.dir or not self.keep_audio or not wav.exists():
            return None
        dest = self.dir / "audio" / wav.name
        shutil.copy2(wav, dest)
        return dest.name

    def rel(self, path: Path | None) -> str | None:
        if not path or not self.dir:
            return None
        try:
            return str(path.relative_to(self.dir)).replace("\\", "/")
        except ValueError:
            return str(path)
