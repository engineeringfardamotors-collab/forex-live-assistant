"""Replay جلسه‌ی ضبط‌شده و سنجش دقت (بخش ۱۰).

    python main.py --replay session_data/sessions/20261030_122500           # فراخوانی دوباره‌ی مدل
    python main.py --replay <dir> --cached                                  # پاسخ‌های ضبط‌شده (رایگان؛ برای تست قواعد)
    python main.py --label-template <dir>                                   # ساخت labels.json از سیگنال‌های ثبت‌شده

labels.json (برچسب دستی سیگنال‌های واقعی جلسه):
[
  {"t": "12:41:05", "symbol": "XAUUSD", "side": "BUY", "entry": 3985.5, "sl": 3978, "tps": [3995, 4005]},
  ...
]
معیارها:
  Precision = سیگنال‌های ارسالیِ درست / کل سیگنال‌های ارسالی   (مهم‌ترین)
  Recall    = سیگنال‌های واقعیِ گرفته‌شده / کل سیگنال‌های واقعی
  Latency   = فاصله‌ی زمان اعلام (برچسب) تا تصمیم ارسال
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app import clock
from app.ai import AIProvider
from app.live_watcher import ChartWatcher
from app.market import MarketPriceProvider
from app.publisher import MemoryPublisher
from app.symbols import get_spec, to_float
from app.trade_state import TradeBook


def load_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _CachedAI:
    def __init__(self, responses: list[dict[str, Any]]):
        self._responses = responses
        self._i = 0

    async def extract_live_events(self, image_path, user_text):  # noqa: ANN001
        r = self._responses[self._i] if self._i < len(self._responses) else {"frame": {}, "events": []}
        self._i += 1
        return r, ""


class _ReplayMarket(MarketPriceProvider):
    """بازار در Replay: فقط قیمت نمودار (منبع مستقل تاریخی نداریم) یا قیمت‌های ضبط‌شده."""

    def __init__(self, cfg: dict[str, Any], recorded: list[dict[str, Any]]):
        super().__init__({**cfg, "market": {**cfg.get("market", {}), "sources": ["chart"]}})
        self.recorded = recorded

    async def get_price(self, symbol):  # noqa: ANN001
        if not symbol:
            return None, "none", False
        best = None
        for m in self.recorded:
            if m.get("symbol") == symbol and m["t"] <= clock.now():
                best = m
        if best and best.get("price") and best.get("src") not in (None, "chart"):
            return best["price"], f"recorded:{best['src']}", True
        p = self.chart_price(symbol)
        return (p, "chart", False) if p else (None, "none", False)


async def replay(cfg: dict[str, Any], session_dir: Path, cached: bool = False) -> dict[str, Any]:
    events = load_events(session_dir)
    analyses = [e for e in events if e["type"] == "analysis" and e.get("response")]
    markets = []
    for e in events:
        if e["type"] == "decision" and (e.get("market") or {}).get("price"):
            sig = e.get("signal") or {}
            markets.append({"t": e["t"], "symbol": sig.get("symbol"), **e["market"]})

    pub = MemoryPublisher(cfg)
    trades = TradeBook(cfg, pub)
    ai = _CachedAI([a["response"] for a in analyses]) if cached else AIProvider(cfg)
    rcfg = {**cfg, "recording": {"enabled": False}}
    watcher = ChartWatcher(rcfg, ai, pub, Path("session_data/replay_tmp"),  # type: ignore[arg-type]
                           market=_ReplayMarket(cfg, markets), trades=trades)
    watcher._running = False
    decisions: list[dict[str, Any]] = []

    from PIL import Image

    for e in events:
        clock.set_time(e["t"])
        if e["type"] == "transcript":
            watcher.add_transcript(e["text"], e.get("t0"), e.get("t1"))
        elif e["type"] == "analysis" and e.get("response") is not None:
            frame_file = e.get("frame")
            if cached:
                data, _ = await ai.extract_live_events(None, "")
            else:
                img_path = session_dir / frame_file if frame_file else None
                from app.prompts import build_live_user_message

                msg = build_live_user_message(watcher.transcript_window(), trades.open_trades_text(),
                                              "(replay)", watcher.last_symbol, img_path is not None, "نامعلوم")
                data, _ = await ai.extract_live_events(img_path if img_path and img_path.exists() else None, msg)
            before = len(pub.sent)
            await watcher.handle_model_output(data)
            for m in pub.sent[before:]:
                decisions.append({"t": e["t"], **m})
    clock.set_time(None)

    published = []
    for t in trades.trades.values():
        if any(h["event"] == "published" for h in t.history):
            published.append({"id": t.id, "t": t.created_at, **_core(t.signal.to_dict())})
    report: dict[str, Any] = {
        "session": str(session_dir), "mode": "cached" if cached else "model",
        "analyses": len(analyses), "published": published,
        "trade_stats": trades.summary(), "messages": len(pub.sent),
    }
    labels_file = session_dir / "labels.json"
    if labels_file.exists():
        labels = json.loads(labels_file.read_text(encoding="utf-8"))
        report["metrics"] = score(published, labels, session_dir)
    out = session_dir / f"replay_{'cached' if cached else 'model'}_{datetime.now():%H%M%S}.json"
    out.write_text(json.dumps({"report": report, "messages": pub.sent}, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    report["output"] = str(out)
    return report


def _core(sig: dict[str, Any]) -> dict[str, Any]:
    entry = sig.get("entry")
    if entry is None and sig.get("entry_low") is not None and sig.get("entry_high") is not None:
        entry = (sig["entry_low"] + sig["entry_high"]) / 2
    return {"symbol": sig.get("symbol"), "side": sig.get("side"), "entry": entry,
            "sl": sig.get("sl"), "tps": sig.get("tps") or []}


def _label_time(label: dict[str, Any], session_dir: Path) -> float | None:
    t = label.get("t")
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str) and ":" in t:
        day = datetime.strptime(session_dir.name[:8], "%Y%m%d")
        hh, mm, *ss = (int(x) for x in t.split(":"))
        return day.replace(hour=hh, minute=mm, second=ss[0] if ss else 0).timestamp()
    return None


def matches(pred: dict[str, Any], label: dict[str, Any], tol_pips: float = 5) -> bool:
    if pred.get("symbol") != label.get("symbol") or pred.get("side") != str(label.get("side", "")).upper():
        return False
    spec = get_spec(label["symbol"])
    tol = spec.pip_size * tol_pips if spec.kind == "forex" else max(spec.pip_size * tol_pips, 0.5)
    for f in ("entry", "sl"):
        lv, pv = to_float(label.get(f)), to_float(pred.get(f))
        if lv is not None and (pv is None or abs(lv - pv) > tol):
            return False
    return True


def score(published: list[dict[str, Any]], labels: list[dict[str, Any]], session_dir: Path) -> dict[str, Any]:
    used: set[int] = set()
    correct, latencies, wrong = 0, [], []
    for p in published:
        hit = next((i for i, lab in enumerate(labels) if i not in used and matches(p, lab)), None)
        if hit is None:
            wrong.append(p)
            continue
        used.add(hit)
        correct += 1
        lt = _label_time(labels[hit], session_dir)
        if lt:
            latencies.append(p["t"] - lt)
    missed = [lab for i, lab in enumerate(labels) if i not in used]
    lat = sorted(latencies)
    return {
        "precision": round(correct / len(published), 3) if published else None,
        "recall": round(correct / len(labels), 3) if labels else None,
        "published": len(published), "labels": len(labels), "correct": correct,
        "false_positives": wrong, "missed": missed,
        "latency_median_sec": round(lat[len(lat) // 2], 1) if lat else None,
    }


def label_template(session_dir: Path) -> Path:
    """پیش‌نویس labels.json از تصمیم‌های ثبت‌شده؛ باید دستی بازبینی و اصلاح شود."""
    rows = []
    for e in load_events(session_dir):
        if e["type"] == "decision" and e.get("signal") and e.get("result") in {"published", "review", "updated"}:
            s = _core(e["signal"])
            rows.append({"t": datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S"), **s,
                         "_auto": e.get("result"), "_check": "درست است؟ اگر نه حذف/اصلاح کنید"})
    out = session_dir / "labels.template.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
