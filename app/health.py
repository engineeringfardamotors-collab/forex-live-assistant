"""پایش سلامت ضبط (فقط هشدار؛ هرگز جلسه را تمام نمی‌کند).

هشدارها:
  WINDOW_MINIMIZED   پنجره‌ی Chrome کوچک شده (mss چیز دیگری را می‌گیرد)
  WINDOW_MOVED       پنجره جابه‌جا یا تغییر اندازه داده شده (ناحیه‌ی چارت دیگر روی چارت نیست)
  WINDOW_MISSING     پنجره‌ی مرورگر پیدا نمی‌شود
  REGION_OUTSIDE     ناحیه‌ی چارت بیرون از پنجره‌ی مرورگر است
  OCCLUDED           پنجره‌ی دیگری روی ناحیه‌ی چارت است
  FRAME_BLACK        تصویر ناحیه بیش از N ثانیه سیاه/یکدست
  FRAME_FROZEN       تصویر ناحیه بیش از N ثانیه کاملاً ثابت
  SCREEN_LOCKED      صفحه قفل است (Win+L)
  SLEEP              سیستم در حال رفتن به خواب است / از خواب برگشته
  SLEEP_GUARD_OFF    جلوگیری از خواب فعال نشد
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from app import winapi
from app.region_picker import normalize_region
from app.utils import get_logger

MESSAGES = {
    "WINDOW_MINIMIZED": "پنجره‌ی Chrome کوچک (Minimize) شده؛ چارت ضبط نمی‌شود",
    "WINDOW_MOVED": "پنجره‌ی Chrome جابه‌جا یا تغییر اندازه داده شده؛ ناحیه‌ی چارت ممکن است اشتباه باشد "
                    "(پنجره را برگردانید یا دوباره --pick-region)",
    "WINDOW_MISSING": "پنجره‌ی مرورگر جلسه پیدا نمی‌شود",
    "REGION_OUTSIDE": "ناحیه‌ی چارت (live.region) بیرون از پنجره‌ی Chrome است",
    "OCCLUDED": "پنجره‌ی دیگری روی ناحیه‌ی چارت است",
    "FRAME_BLACK": "تصویر ناحیه‌ی چارت سیاه/یکدست است",
    "FRAME_FROZEN": "تصویر ناحیه‌ی چارت ثابت مانده (فریز یا قطع اشتراک صفحه؟)",
    "SCREEN_LOCKED": "صفحه قفل است؛ ضبط تصویر کار نمی‌کند",
    "SLEEP": "سیستم به خواب می‌رود یا از خواب برگشته؛ ضبط قطع شده/می‌شود",
    "SLEEP_GUARD_OFF": "جلوگیری از خواب ویندوز فعال نشد؛ تنظیمات Power را بررسی کنید",
}


class CaptureHealth:
    def __init__(self, cfg: dict[str, Any], frame_info: Callable[[], Any] | None = None,
                 notify: Callable[[str], None] | None = None):
        h = cfg.get("health", {})
        live = cfg.get("live", {})
        self.interval = float(h.get("check_interval_sec", 5))
        self.black_after = float(h.get("black_after_sec", 30))
        self.frozen_after = float(h.get("frozen_after_sec", 30))
        self.move_tolerance = int(h.get("move_tolerance_px", 8))
        self.prevent_sleep = bool(h.get("prevent_sleep", True))
        self.region = normalize_region(live.get("region"))
        self.meta = live.get("region_meta") or {}
        self.frame_info = frame_info
        self.notify = notify
        self.log = get_logger("health", cfg["logging"]["level"], cfg["logging"]["dir"])
        self.active: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self._hwnd: int | None = None
        self._baseline: tuple[int, int, int, int] | None = None
        self._black_since: float | None = None
        self._frozen_since: float | None = None
        self._sleep_event_at: float | None = None
        self._task: asyncio.Task | None = None
        self._power: winapi.PowerEvents | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------- warnings ----------
    def _set(self, code: str, on: bool, detail: str = "") -> None:
        now = time.time()
        if on and code not in self.active:
            text = MESSAGES.get(code, code) + (f" ({detail})" if detail else "")
            self.active[code] = {"code": code, "message": text, "since": now}
            self.history.append({"t": now, "code": code, "event": "on", "message": text})
            self.log.warning("⚠️ سلامت ضبط: %s", text)
            if self.notify:
                self.notify(f"⚠️ {text}")
        elif on:
            self.active[code]["detail"] = detail
        elif not on and code in self.active:
            dur = now - self.active.pop(code)["since"]
            self.history.append({"t": now, "code": code, "event": "off", "duration": round(dur)})
            self.log.info("✔ برطرف شد: %s (پس از %.0f ثانیه)", MESSAGES.get(code, code), dur)
        self.history = self.history[-100:]

    def warnings(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{**w, "for_sec": round(now - w["since"])} for w in self.active.values()]

    # ---------- checks ----------
    def _on_power(self, kind: str) -> None:
        self._sleep_event_at = time.time()
        detail = "در حال رفتن به خواب" if kind == "suspend" else "برگشت از خواب"
        if self._loop:
            self._loop.call_soon_threadsafe(self._set, "SLEEP", True, detail)
        else:
            self._set("SLEEP", True, detail)

    def set_baseline(self, rect: tuple[int, int, int, int] | None = None) -> None:
        if rect:
            self._baseline = rect
        elif self.meta.get("win_w"):
            m = self.meta
            self._baseline = (int(m["win_left"]), int(m["win_top"]),
                              int(m["win_left"]) + int(m["win_w"]), int(m["win_top"]) + int(m["win_h"]))

    def check_window(self, win: winapi.WindowInfo | None) -> None:
        if win is None:
            self._set("WINDOW_MISSING", True)
            for c in ("WINDOW_MINIMIZED", "WINDOW_MOVED", "REGION_OUTSIDE", "OCCLUDED"):
                self._set(c, False)
            return
        self._set("WINDOW_MISSING", False)
        self._set("WINDOW_MINIMIZED", win.minimized)
        if win.minimized:
            return
        if self._baseline is None:
            self._baseline = win.rect
        moved = any(abs(a - b) > self.move_tolerance for a, b in zip(win.rect, self._baseline))
        self._set("WINDOW_MOVED", moved,
                  f"قبلی {self._baseline}، فعلی {win.rect}" if moved else "")
        if self.region:
            r = self.region
            l, t, rr, b = win.rect
            outside = (r["left"] < l - self.move_tolerance or r["top"] < t - self.move_tolerance
                       or r["left"] + r["width"] > rr + self.move_tolerance
                       or r["top"] + r["height"] > b + self.move_tolerance)
            self._set("REGION_OUTSIDE", outside)
            fg = winapi.foreground_window()
            occluded = False
            if fg and fg.hwnd != win.hwnd and not fg.minimized and fg.width > 0:
                name = winapi.process_name(fg.pid)
                ignore = {"python.exe", "pythonw.exe", "explorer.exe", "searchhost.exe", "shellexperiencehost.exe"}
                if name not in ignore:
                    ix = min(fg.rect[2], r["left"] + r["width"]) - max(fg.rect[0], r["left"])
                    iy = min(fg.rect[3], r["top"] + r["height"]) - max(fg.rect[1], r["top"])
                    occluded = ix > 0 and iy > 0 and ix * iy > 0.2 * r["width"] * r["height"]
                    detail = f"{name}: {fg.title[:40]}"
            self._set("OCCLUDED", occluded, detail if occluded else "")

    def check_frame(self, info: Any, now: float | None = None) -> None:
        now = now or time.time()
        if info is None:
            return
        black = (not info.valid) and info.reason == "blank"
        self._black_since = (self._black_since or now) if black else None
        self._set("FRAME_BLACK", bool(self._black_since and now - self._black_since >= self.black_after),
                  f"{now - self._black_since:.0f}s" if self._black_since else "")
        frozen = getattr(info, "frozen_sec", 0) or 0
        self._set("FRAME_FROZEN", frozen >= self.frozen_after, f"{frozen:.0f}s")

    def check_once(self) -> None:
        try:
            self._set("SCREEN_LOCKED", winapi.screen_locked())
        except Exception:  # noqa: BLE001
            pass
        try:
            win = None
            if self._hwnd:
                win = next((w for w in winapi.list_browser_windows() if w.hwnd == self._hwnd), None)
            if win is None:
                win = winapi.find_browser_window()
                self._hwnd = win.hwnd if win else None
            self.check_window(win)
        except Exception as e:  # noqa: BLE001
            self.log.debug("بررسی پنجره ناموفق: %s", e)
        if self.frame_info:
            self.check_frame(self.frame_info())
        if self._sleep_event_at and time.time() - self._sleep_event_at > 120:
            self._sleep_event_at = None
            self._set("SLEEP", False)

    async def _run(self) -> None:
        while True:
            self.check_once()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.active.clear()
        self._hwnd = None
        self._baseline = None
        self.set_baseline()
        if self.prevent_sleep:
            self._set("SLEEP_GUARD_OFF", not winapi.keep_awake(True))
        self._power = winapi.PowerEvents(self._on_power)
        self._power.start()
        self._task = self._loop.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._power:
            self._power.stop()
            self._power = None
        winapi.keep_awake(False)
