"""تحلیل ارزان فریم‌ها قبل از مدل بینایی (بخش ۴).

- فریم نامعتبر: سیاه/سفید/یکدست (قطع اشتراک صفحه)
- فریز: تصویر در مدت طولانی دقیقاً ثابت
- ناحیه‌ی تغییرکرده: کادر محدودکننده‌ی تغییر برای راهنمایی مدل («فقط رسم‌های جدید»)
- کم‌وزن‌کردن کندل جاری: لبه‌ی راست نمودار که مدام تکان می‌خورد
- تشخیص زوم/اسکرول/تعویض صفحه: تغییر سراسری ≠ رسم جدید
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

SIG_W, SIG_H = 160, 90


@dataclass
class FrameInfo:
    valid: bool
    reason: str = ""
    diff: float = 0.0               # میانگین تغییر وزن‌دار (0..255)
    changed_frac: float = 0.0       # درصد سلول‌های تغییرکرده
    bbox: tuple[float, float, float, float] | None = None  # x0,y0,x1,y1 نسبی (0..1)
    kind: str = "none"              # none | local | global | live_edge
    frozen_sec: float = 0.0

    def hint(self) -> str:
        if self.bbox is None:
            return "تغییر محسوسی نسبت به فریم قبل نیست (رسم‌ها احتمالاً قدیمی‌اند)."
        x0, y0, x1, y1 = self.bbox
        where = _describe_region(x0, y0, x1, y1)
        kinds = {
            "local": "تغییر محلی (احتمالاً رسم/برچسب جدید)",
            "global": "تغییر سراسری (زوم، اسکرول، تعویض نماد یا صفحه)؛ رسم‌ها لزوماً جدید نیستند",
            "live_edge": "فقط لبه‌ی راست (کندل جاری) تغییر کرده",
        }
        return (f"{kinds.get(self.kind, self.kind)} در {where} — کادر نسبی x:{x0:.2f}-{x1:.2f}، "
                f"y:{y0:.2f}-{y1:.2f}، {self.changed_frac:.0%} از صفحه.")


def _describe_region(x0: float, y0: float, x1: float, y1: float) -> str:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    h = "چپ" if cx < 0.33 else "وسط" if cx < 0.66 else "راست"
    v = "بالا" if cy < 0.33 else "میانه" if cy < 0.66 else "پایین"
    return f"{v}-{h} تصویر"


class FrameAnalyzer:
    def __init__(self, cfg: dict):
        live = cfg.get("live", {})
        self.threshold = float(live.get("change_threshold", 1.8))
        self.cell_threshold = float(live.get("cell_threshold", 12))
        self.min_std = float(live.get("min_frame_std", 5))
        self.live_edge_frac = float(live.get("live_edge_frac", 0.10))
        self.live_edge_weight = float(live.get("live_edge_weight", 0.25))
        self.global_frac = float(live.get("global_change_frac", 0.45))
        self.frozen_after = float(live.get("frozen_after_sec", 90))
        self._prev: np.ndarray | None = None
        self._last_any_change = time.time()
        w = np.ones((SIG_H, SIG_W), dtype=np.float32)
        edge = int(SIG_W * (1 - self.live_edge_frac))
        w[:, edge:] = self.live_edge_weight
        self._weights = w
        self._edge_col = edge

    @staticmethod
    def signature(img) -> np.ndarray:
        return np.asarray(img.convert("L").resize((SIG_W, SIG_H)), dtype=np.float32)

    def analyze(self, img) -> FrameInfo:
        sig = self.signature(img)
        std = float(sig.std())
        if std < self.min_std:
            self._prev = sig
            return FrameInfo(False, "blank", frozen_sec=0)
        prev, self._prev = self._prev, sig
        if prev is None:
            self._last_any_change = time.time()
            return FrameInfo(True, kind="global", diff=255, changed_frac=1.0, bbox=(0, 0, 1, 1))

        raw = np.abs(sig - prev)
        if float(raw.max()) < 1.0:
            frozen = time.time() - self._last_any_change
            info = FrameInfo(True, "frozen" if frozen > self.frozen_after else "", frozen_sec=frozen)
            if frozen > self.frozen_after:
                info.valid = False
            return info
        self._last_any_change = time.time()

        diff = float((raw * self._weights).sum() / self._weights.sum())
        mask = raw > self.cell_threshold
        frac = float(mask.mean())
        info = FrameInfo(True, diff=diff, changed_frac=frac)
        if not mask.any():
            return info
        ys, xs = np.where(mask)
        info.bbox = (xs.min() / SIG_W, ys.min() / SIG_H, (xs.max() + 1) / SIG_W, (ys.max() + 1) / SIG_H)
        if frac >= self.global_frac:
            info.kind = "global"
        elif xs.min() >= self._edge_col - 2:
            info.kind = "live_edge"
        else:
            info.kind = "local"
        return info

    def is_event(self, info: FrameInfo) -> bool:
        return info.valid and info.kind in {"local", "global"} and info.diff >= self.threshold
