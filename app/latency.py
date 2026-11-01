"""تفکیک تأخیر هر سیگنال به مراحل خط لوله.

    گفته شدن ─► [audio_wait] ─► قطعه آماده ─► [stt_queue+stt] ─► متن ─► [trigger_wait]
        ─► [vision] ─► پاسخ مدل ─► [decision] ─► صف ارسال ─► [telegram] ─► پیام در کانال

audio_wait   : از شروع قطعه‌ی صوتی حاوی جمله تا بسته‌شدن قطعه (سقف = طول قطعه)
stt          : از آماده شدن قطعه تا برگشت متن (شامل صف و فراخوانی API)
trigger_wait : از آماده شدن متن تا شروع فراخوانی مدل بینایی (cooldown/قفل)
vision       : فراخوانی مدل بینایی
decision     : ساخت سیگنال، اعتبارسنجی، مسیر‌یابی و صف‌کردن پیام
telegram     : از صف شدن تا تأیید ارسال Bot API
"""
from __future__ import annotations

import time
from typing import Any, Callable

STAGES = ["audio_wait", "stt", "trigger_wait", "vision", "decision", "telegram"]
STAGE_FA = {
    "audio_wait": "انتظار قطعه‌ی صوتی", "stt": "تبدیل گفتار به متن", "trigger_wait": "انتظار تا تحلیل",
    "vision": "مدل بینایی", "decision": "اعتبارسنجی و تصمیم", "telegram": "ارسال تلگرام", "total": "کل",
}


def compute_stages(audio: dict[str, float] | None, vision_start: float, vision_end: float,
                   decision_end: float) -> dict[str, float | None]:
    """audio = {t0, t1, stt_start, stt_end} از قطعه‌ی صوتیِ جمله‌ی شاهد (یا None اگر فقط تصویر)."""
    s: dict[str, float | None] = {k: None for k in STAGES}
    if audio and audio.get("t0") is not None and audio.get("t1") is not None:
        s["audio_wait"] = max(0.0, audio["t1"] - audio["t0"])
        if audio.get("stt_end") is not None:
            s["stt"] = max(0.0, audio["stt_end"] - audio["t1"])
            s["trigger_wait"] = max(0.0, vision_start - audio["stt_end"])
    s["vision"] = max(0.0, vision_end - vision_start)
    s["decision"] = max(0.0, decision_end - vision_end)
    return {k: (round(v, 3) if v is not None else None) for k, v in s.items()}


def total_of(stages: dict[str, float | None]) -> float:
    return round(sum(v for v in stages.values() if v is not None), 3)


class LatencyTracker:
    def __init__(self, write: Callable[..., None] | None = None, keep: int = 200):
        self.write = write
        self.keep = keep
        self.records: list[dict[str, Any]] = []
        self._by_msg: dict[str, dict[str, Any]] = {}

    def add(self, trade_id: str | None, result: str, stages: dict[str, float | None],
            msg_id: str | None = None, enqueued_at: float | None = None) -> dict[str, Any]:
        rec = {"trade_id": trade_id, "result": result, "stages": dict(stages), "msg_id": msg_id,
               "enqueued_at": enqueued_at or time.time(), "complete": msg_id is None}
        rec["stages"]["total"] = total_of(rec["stages"])
        self.records.append(rec)
        self.records = self.records[-self.keep:]
        if msg_id:
            self._by_msg[msg_id] = rec
        else:
            self._emit(rec)
        return rec

    def on_sent(self, msg_id: str, sent_at: float) -> None:
        rec = self._by_msg.pop(msg_id, None)
        if rec is None:
            return
        rec["stages"]["telegram"] = round(max(0.0, sent_at - rec["enqueued_at"]), 3)
        rec["stages"]["total"] = total_of({k: v for k, v in rec["stages"].items() if k != "total"})
        rec["complete"] = True
        self._emit(rec)

    def _emit(self, rec: dict[str, Any]) -> None:
        if self.write:
            self.write("latency", trade_id=rec["trade_id"], result=rec["result"], stages=rec["stages"])

    def summary(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k in STAGES + ["total"]:
            vals = [r["stages"][k] for r in self.records if r["stages"].get(k) is not None]
            if vals:
                out[k] = {"label": STAGE_FA[k], "avg": round(sum(vals) / len(vals), 2),
                          "max": round(max(vals), 2), "n": len(vals)}
        return out

    def biggest_stage(self) -> str | None:
        s = {k: v for k, v in self.summary().items() if k != "total"}
        return max(s, key=lambda k: s[k]["avg"]) if s else None
