"""موتور زنده‌ی جلسه: تصویر + گفتار → رویداد معاملاتی → اعتبارسنجی → کانال.

جریان:
  فریم هر چند ثانیه ─► FrameAnalyzer (نامعتبر/فریز/محلی/سراسری)
  گفتار هر قطعه   ─► نرمال‌سازی + حذف همپوشانی + کلیدواژه
        └──── رویداد (تغییر محلی چارت یا کلیدواژه‌ی گفتار) ────┐
                                                                ▼
  مدل بینایی (prompts.LIVE_EXTRACTION_SYSTEM) ─► events[] با intent
        ├─ ACTUAL_ENTRY / PENDING_ORDER / CONDITIONAL ─► build_signal ─► پیش‌نویس ─► validate ─► route
        ├─ MANAGEMENT ─► Reply به سیگنال اصلی
        ├─ CANCEL     ─► لغو
        └─ بقیه       ─► فقط ثبت در ضبط جلسه
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app import clock
from app.ai import AIProvider, encode_image_data_url
from app.frames import FrameAnalyzer, FrameInfo
from app.latency import STAGE_FA, LatencyTracker, compute_stages
from app.lexicon import is_trigger, merge_overlap, normalize_trading_text, speech_flags
from app.market import MarketPriceProvider
from app.prompts import PROMPT_VERSION, build_live_user_message
from app.publisher import TelegramPublisher
from app.recorder import SessionRecorder
from app.signal_model import (
    ENTRY_INTENTS, REVIEW, BuildContext, Issue, build_signal, route, validate,
)
from app.symbols import fmt_price, get_spec, normalize_symbol, to_float
from app.trade_state import TradeBook
from app.utils import get_logger


def monitor_for_region(monitors: list[dict[str, int]],
                       region: dict[str, Any] | None) -> dict[str, int] | None:
    """مانیتورِ حاوی ناحیه‌ی چارت: اول محاصره‌ی کامل، وگرنه بیشترین هم‌پوشانی.

    `live.region` مختصات فیزیکی مجازی‌سازی‌شده است (با left/top مانیتور)؛ روی چند مانیتور
    ناحیه ممکن است روی مانیتور دوم باشد و گرفتن کورکورانه‌ی مانیتور اول اشتباه می‌شد.
    """
    if not region:
        return None
    rl, rt = int(region.get("left", 0)), int(region.get("top", 0))
    rr, rb = rl + int(region.get("width", 0)), rt + int(region.get("height", 0))
    best: dict[str, int] | None = None
    best_overlap = 0
    for m in monitors:
        ml, mt = int(m.get("left", 0)), int(m.get("top", 0))
        mr, mb = ml + int(m.get("width", 0)), mt + int(m.get("height", 0))
        if rl >= ml and rt >= mt and rr <= mr and rb <= mb:
            return m
        ox = max(0, min(rr, mr) - max(rl, ml))
        oy = max(0, min(rb, mb) - max(rt, mt))
        if ox * oy > best_overlap:
            best, best_overlap = m, ox * oy
    return best


def clip_region_to_monitor(region: dict[str, Any], mon: dict[str, int]) -> dict[str, int]:
    """ناحیه‌ی نیمه‌بیرون‌زده را به هم‌پوشانی‌اش با مانیتور محدود می‌کند."""
    ml, mt = int(mon.get("left", 0)), int(mon.get("top", 0))
    mr, mb = ml + int(mon.get("width", 0)), mt + int(mon.get("height", 0))
    l = max(int(region.get("left", 0)), ml)
    t = max(int(region.get("top", 0)), mt)
    r = min(int(region.get("left", 0)) + int(region.get("width", 0)), mr)
    b = min(int(region.get("top", 0)) + int(region.get("height", 0)), mb)
    return {"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)}


class ChartWatcher:
    def __init__(
        self,
        cfg: dict[str, Any],
        ai: AIProvider,
        publisher: TelegramPublisher,
        frames_dir: Path,
        recorder: SessionRecorder | None = None,
        market: MarketPriceProvider | None = None,
        trades: TradeBook | None = None,
    ):
        self.cfg = cfg
        live = cfg.get("live", {})
        self.interval = float(live.get("scan_interval_sec", 3))
        self.cooldown = float(live.get("vision_cooldown_sec", 10))
        self.speech_cooldown = float(live.get("speech_cooldown_sec", 4))
        self.context_sec = float(live.get("context_sec", 60))
        self.region = live.get("region")
        self.image_max_width = int(live.get("vision_image_max_width", 1400))
        self.frames_dir = frames_dir
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.ai = ai
        self.pub = publisher
        self.recorder = recorder or SessionRecorder({"recording": {"enabled": False}})
        self.market = market or MarketPriceProvider(cfg)
        self.trades = trades or TradeBook(cfg, publisher)
        self.frames = FrameAnalyzer(cfg)
        self.aliases = (cfg.get("symbols") or {}).get("aliases") or {}
        self.spec_overrides = (cfg.get("symbols") or {}).get("overrides") or {}
        self.closed_policy = (cfg.get("market") or {}).get("closed_policy", "review")
        self.stale_pct = float((cfg.get("market") or {}).get("stale_stream_pct", 1.0))
        self.log = get_logger("watcher", cfg["logging"]["level"], cfg["logging"]["dir"])

        self._running = False
        self._task: asyncio.Task | None = None
        self._vision_task: asyncio.Task | None = None   # تحلیل پس‌زمینه؛ حلقه‌ی پایش را بلاک نمی‌کند
        self._pending_chart = False                     # رویداد چارت که حین تحلیل قبلی آمده بود
        self._tls = threading.local()                   # نمونه‌ی mss per-thread (ساختن مکرر پرهزینه است)
        self._lock = asyncio.Lock()
        self._speech_trigger = asyncio.Event()
        self._transcript: list[tuple[float, float, str]] = []   # (t0, t1, text)
        self._audio_timing: dict[tuple[float, float], dict[str, float | None]] = {}
        self._vision_window: tuple[float, float] | None = None
        self.latency = LatencyTracker(self.recorder.write)
        publisher.on_sent = self.latency.on_sent
        self._last_vision = 0.0
        self._last_img = None
        self._last_info: FrameInfo | None = None
        self.last_symbol: str | None = None
        self.last_timeframe: str | None = None
        self.stale_stream = False

        self.chart_events = 0
        self.speech_triggers = 0
        self.vision_calls = 0
        self.vision_errors = 0
        self.prefilter_skips = 0
        self.invalid_since: float | None = None
        self.last_change_at = 0.0
        self.last_speech_at = 0.0
        self.last_signal_at = 0.0
        self.latencies: list[float] = []

    # ---------- transcript ----------
    def add_transcript(self, text: str, t0: float | None = None, t1: float | None = None,
                       audio_file: str | None = None, stt_start: float | None = None,
                       stt_end: float | None = None) -> None:
        if not text:
            return
        t1 = t1 or clock.now()
        t0 = t0 or t1 - 10
        prev = self._transcript[-1][2] if self._transcript else ""
        text = merge_overlap(prev, normalize_trading_text(text))
        if not text.strip():
            return
        self._transcript.append((t0, t1, text))
        self._audio_timing[(t0, t1)] = {"t0": t0, "t1": t1, "stt_start": stt_start,
                                        "stt_end": stt_end if stt_end is not None else clock.now()}
        cutoff = clock.now() - 600
        self._transcript = [x for x in self._transcript if x[1] >= cutoff]
        self._audio_timing = {k: v for k, v in self._audio_timing.items() if k[1] >= cutoff}
        if len(text.split()) >= 3:
            self.last_speech_at = t1
        self.recorder.write("transcript", text=text, t0=t0, t1=t1, audio=audio_file,
                            stt_sec=round(stt_end - stt_start, 2) if stt_start and stt_end else None)
        if self._running and is_trigger(text):
            self.speech_triggers += 1
            self.log.info("اصطلاح معاملاتی در گفتار: «%s»", text[:80])
            self._speech_trigger.set()

    def transcript_window(self, seconds: float | None = None) -> str:
        now = clock.now()
        cutoff = now - (seconds or self.context_sec)
        rows = [f"[{int(t0 - now)}s] {txt}" for t0, t1, txt in self._transcript if t1 >= cutoff]
        return "\n".join(rows)

    def recent_text(self, seconds: float | None = None) -> str:
        cutoff = clock.now() - (seconds or self.context_sec)
        return " ".join(txt for _, t1, txt in self._transcript if t1 >= cutoff)

    # ---------- screen ----------
    def _mss_client(self):
        """نمونه‌ی mss هر thread فقط یک بار ساخته می‌شود (ساختن هر ۳ ثانیه پرهزینه است)."""
        import mss

        client = getattr(self._tls, "mss", None)
        if client is None:
            client = mss.mss()
            self._tls.mss = client
        return client

    def _grab(self):
        from PIL import Image

        last_err: Exception | None = None
        for _attempt in (0, 1):
            try:
                sct = self._mss_client()
                monitors = sct.monitors
                area = monitors[1] if len(monitors) > 1 else monitors[0]
                if self.region:
                    mon = monitor_for_region(monitors[1:] if len(monitors) > 1 else [], self.region)
                    if mon is not None:
                        # اگر ناحیه کمی بیرون مانیتور زده باشد، به همان مانیتور کلیپ می‌شود
                        area = clip_region_to_monitor(self.region, mon)
                    elif not getattr(self, "_region_warned", False):
                        self._region_warned = True
                        self.log.warning("live.region با هیچ مانیتوری جور نیست (رزولوشن عوض شده؟)؛ کل صفحه گرفته می‌شود")
                        self.pub.notify_admin("live.region با صفحه‌نمایش جور نیست؛ کل صفحه پایش می‌شود.")
                shot = sct.grab(area)
                return Image.frombytes("RGB", shot.size, shot.rgb)
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._tls.mss = None   # نمونه‌ی خراب (مثلاً DC ازدست‌رفته)؛ بار بعد از نو ساخته می‌شود
        raise last_err  # type: ignore[misc]

    async def _loop(self) -> None:
        self.log.info("پایش زنده فعال شد (هر %.1fs؛ دستورالعمل %s)", self.interval, PROMPT_VERSION)
        last_tick = 0.0
        while self._running:
            try:
                img = await asyncio.to_thread(self._grab)
                await self.on_frame(img)
                # رویداد چارت که حین تحلیل قبلی آمده بود: بلافاصله پس از آزادشدن مدل اجرا می‌شود
                # (cooldown داخل process همچنان رعایت می‌شود)
                if self._pending_chart and self._vision_idle():
                    self._pending_chart = False
                    info = self._last_info
                    self._maybe_process(img if info and info.valid else None, "chart", info)
                if time.time() - last_tick > 15:
                    last_tick = time.time()
                    await self._tick()
            except Exception as e:  # noqa: BLE001
                self.log.warning("خطا در پایش چارت: %s", e)
            try:
                await asyncio.wait_for(self._speech_trigger.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def on_frame(self, img) -> None:
        info = await asyncio.to_thread(self.frames.analyze, img)
        self._last_info = info
        forced = self._speech_trigger.is_set()
        self._speech_trigger.clear()
        if info.valid:
            self._last_img = img
            self.invalid_since = None
        elif self.invalid_since is None:
            self.invalid_since = clock.now()
            self.log.info("فریم نامعتبر (%s)؛ تحلیل تصویری متوقف شد", info.reason)
            self.recorder.write("note", text=f"invalid frame: {info.reason}")
        if self.frames.is_event(info):
            self.chart_events += 1
            if info.kind == "local":
                self.last_change_at = clock.now()
            self._maybe_process(img if info.valid else None, "chart", info)
        elif forced:
            self._maybe_process(img if info.valid else None, "speech", info)

    def _vision_idle(self) -> bool:
        return self._vision_task is None or self._vision_task.done()

    async def _process_safe(self, img, reason: str, info: FrameInfo | None) -> None:
        """تحلیل پس‌زمینه با ثبت خطا — استثنای فرارکننده در تسک پس‌زمینه بی‌صدا گم نمی‌شود."""
        try:
            await self.process(img, reason, info)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.log.warning("خطای غیرمنتظره در تحلیل بینایی: %s", e)

    def _maybe_process(self, img, reason: str, info: FrameInfo | None) -> asyncio.Task | None:
        """تحلیل بینایی را در پس‌زمینه اجرا می‌کند تا پایش فریم‌ها حین مدل متوقف نشود.

        اگر تحلیل قبلی هنوز در جریان است یا cooldown تصویری فعال است: محرک گفتاری مثل قبل
        دوباره مسلح می‌شود و رویداد تصویری به‌جای گم‌شدن «در انتظار» علامت می‌خورد تا حلقه
        بلافاصله پس از اتمام تحلیل جاری / پایان cooldown، آن را با تازه‌ترین فریم اجرا کند.
        رویدادهای یک بازه ادغام می‌شوند؛ پس نرخ تحلیل همچنان سقف `vision_cooldown_sec` دارد.
        """
        if not self._vision_idle():
            if reason == "speech":
                self._speech_trigger.set()
            else:
                self._pending_chart = True
            return None
        if reason != "speech" and clock.now() - self._last_vision < self.cooldown:
            self._pending_chart = True   # حین cooldown: پس از پایان آن با تازه‌ترین فریم اجرا می‌شود
            return None
        self._vision_task = asyncio.get_running_loop().create_task(self._process_safe(img, reason, info))
        return self._vision_task

    async def _tick(self) -> None:
        prices = {}
        for t in self.trades.open_trades():
            p, _, _ = await self.market.get_price(t.signal.symbol)
            if p:
                prices[t.signal.symbol] = p
        self.trades.tick(prices)

    # ---------- core ----------
    async def process(self, img, reason: str, info: FrameInfo | None = None) -> None:
        now = clock.now()
        cd = self.speech_cooldown if reason == "speech" else self.cooldown
        if now - self._last_vision < cd:
            if reason == "speech":
                self._speech_trigger.set()   # بعد از پایان cooldown دوباره
            else:
                self._pending_chart = True   # رویداد چارت حین cooldown حذف نمی‌شود
            return
        if self._lock.locked():
            if reason == "speech":
                self._speech_trigger.set()
            else:
                self._pending_chart = True
            return
        async with self._lock:
            self._last_vision = now
            await self._process_locked(img, reason, info)

    def _prepare_frame(self, img, reason: str) -> tuple[Path | None, str | None]:
        """ذخیره‌ی فریم (ضبط جلسه) + فشرده‌سازی JPEG برای مدل؛ همه در thread اجرا می‌شود
        تا کدگذاری PNG/JPEG (صدها میلی‌ثانیه) event loop را بلاک نکند.
        خروجی: (مسیر فریم، data-URL آماده) — مدل دیگر تصویر را از دیسک بازخوانی نمی‌کند."""
        frame_path = self.recorder.save_frame(img, reason)
        if frame_path is None:
            frame_path = Path(tempfile.gettempdir()) / "fla_live_frame.png"
            img.save(frame_path, "PNG")
        return frame_path, encode_image_data_url(img, self.image_max_width)

    async def _process_locked(self, img, reason: str, info: FrameInfo | None) -> None:
        frame_path: Path | None = None
        image_url: str | None = None
        if img is not None:
            frame_path, image_url = await asyncio.to_thread(self._prepare_frame, img, reason)
        ref_price, ref_src, _ = await self.market.get_price(self.last_symbol)
        ref_txt = f"{ref_price} ({ref_src})" if ref_price else "نامعلوم"
        user_msg = build_live_user_message(
            self.transcript_window(), self.trades.open_trades_text(),
            info.hint() if info else "—", self.last_symbol, frame_path is not None, ref_txt,
        )
        started = time.time()
        # پیش‌فیلتر ارزان (اختیاری): فقط برای محرک تصویری؛ محرک گفتاری همیشه به مدل اصلی می‌رسد
        if (reason == "chart" and getattr(self.ai, "precheck_enabled", False) and frame_path is not None
                and not await self.ai.precheck_frame(frame_path)):
            self.prefilter_skips += 1
            self.recorder.write("prefilter", skipped=True, frame=self.recorder.rel(frame_path))
            return
        self.vision_calls += 1
        try:
            data, raw = await self.ai.extract_live_events(frame_path, user_msg, image_url=image_url)
        except Exception as e:  # noqa: BLE001
            self.vision_errors += 1
            detail = f"{type(e).__name__}: {e}".strip(": ")
            self.log.warning("خطای مدل بینایی: %s", detail)
            self.recorder.write("analysis", frame=self.recorder.rel(frame_path), error=detail)
            if self.vision_errors in (3, 20):
                self.pub.notify_admin(f"خطای پیاپی مدل بینایی: {detail}")
            if reason == "speech":
                # خطای گذرا نباید رویداد گفتاری را از دست بدهد؛ پس از cooldown دوباره تلاش می‌شود
                self._speech_trigger.set()
            return
        latency = time.time() - started
        self.recorder.write("analysis", frame=self.recorder.rel(frame_path), reason=reason, request=user_msg,
                            response=data, raw=raw if data.get("parse_error") else None,
                            prompt_version=PROMPT_VERSION, latency=round(latency, 2))
        self._vision_window = (started, time.time())
        try:
            await self.handle_model_output(data)
        finally:
            self._vision_window = None

    async def handle_model_output(self, data: dict[str, Any]) -> list[str]:
        frame = data.get("frame") or {}
        frame_symbol = normalize_symbol(frame.get("symbol_on_chart"), self.aliases)
        frame_type = str(frame.get("type") or "other")
        chart_price = None
        if frame_type == "chart" and frame_symbol:
            if frame_symbol != self.last_symbol and self.last_symbol:
                self.log.info("نماد نمودار عوض شد: %s → %s", self.last_symbol, frame_symbol)
            self.last_symbol = frame_symbol
            if frame.get("timeframe") and frame.get("timeframe") != "null":
                self.last_timeframe = frame.get("timeframe")
            chart_price = to_float(frame.get("current_price"))
            self.market.update_from_chart(frame_symbol, chart_price)
            await self._check_stale_stream(frame_symbol, chart_price)

        results = []
        for ev in data.get("events") or []:
            try:
                results.append(await self._handle_event(ev, frame, frame_symbol))
            except Exception as e:  # noqa: BLE001
                self.log.error("خطا در پردازش رویداد: %s (%s)", e, ev)
        return results

    async def _check_stale_stream(self, symbol: str, chart_price: float | None) -> None:
        if not chart_price:
            return
        price, src, independent = await self.market.get_price(symbol)
        if not independent or not price:
            return
        dev = abs(chart_price - price) / price * 100
        stale = dev > self.stale_pct and self.market.market_open(symbol)
        if stale and not self.stale_stream:
            self.log.warning("قیمت نمودار %s با بازار %s %.2f٪ فاصله دارد؛ احتمال پخش ضبط قدیمی", chart_price, price, dev)
            self.pub.notify_admin(f"⚠️ قیمت روی نمودار ({chart_price}) با بازار ({price}, {src}) {dev:.2f}٪ فاصله دارد. "
                                  "احتمالاً جلسه‌ی ضبط‌شده یا نمودار قدیمی پخش می‌شود؛ همه‌ی سیگنال‌ها به بازبینی می‌روند.")
        self.stale_stream = stale

    async def _handle_event(self, ev: dict[str, Any], frame: dict[str, Any], frame_symbol: str | None) -> str:
        intent = str(ev.get("intent") or "NOT_A_SIGNAL").upper()
        ev_symbol = normalize_symbol(ev.get("symbol"), self.aliases) or frame_symbol or self.last_symbol
        conf = float(ev.get("confidence") or 0) if isinstance(ev.get("confidence"), (int, float)) else 0.5
        mgmt = ev.get("management") or {}

        if intent == "MANAGEMENT" and str(mgmt.get("action") or "").upper() != "REENTRY":
            res = self.trades.handle_management(ev, ev_symbol, conf)
            self.recorder.write("decision", intent=intent, result=res, event=ev)
            return res
        if intent == "CANCEL":
            res = self.trades.handle_cancel(ev, ev_symbol, conf)
            self.recorder.write("decision", intent=intent, result=res, event=ev)
            return res
        if intent == "MANAGEMENT":
            if str(mgmt.get("action") or "").upper() == "REENTRY" and (mgmt.get("price") or mgmt.get("percent")):
                # ورود مجدد همراه پله‌ی حجم («نصف حجم دوباره ۸۰») → entries سیگنال جدید
                pct = to_float(mgmt.get("percent"))
                if pct and pct > 1:
                    pct = pct / 100
                price = to_float(mgmt.get("price"))
                entries = [e for e in (ev.get("entries") or []) if to_float((e or {}).get("price"))]
                if price and not any(to_float(e.get("price")) == price for e in entries):
                    entries.append({"price": price, "portion": float(pct or 0.0)})
                if entries:
                    ev["entries"] = entries
            intent = ev["intent"] = "ACTUAL_ENTRY"
        if intent not in ENTRY_INTENTS:
            self.recorder.write("decision", intent=intent, result="ignored", event=ev)
            if intent in {"HYPOTHETICAL", "REVIEW_PAST"} and ev.get("side"):
                self.log.info("رد: %s (%s)", intent, ev.get("evidence_quote", "")[:80])
            return "ignored"

        market_price, msrc, independent = await self.market.get_price(ev_symbol)
        ctx = BuildContext(ref_price=market_price, frame_symbol=frame.get("symbol_on_chart"),
                           last_symbol=self.last_symbol, timeframe=self.last_timeframe,
                           symbol_aliases=self.aliases, spec_overrides=self.spec_overrides)
        sig, issues = build_signal(ev, ctx)
        if sig is None:
            self.recorder.write("decision", intent=intent, result="no_symbol", event=ev)
            return "no_symbol"
        sig.spoken_at = self._find_spoken_time(sig.evidence)
        if sig.symbol != ev_symbol and market_price is not None:
            market_price, msrc, independent = await self.market.get_price(sig.symbol)

        issues += self._safety_issues(sig, ev, frame)
        sig = self.trades.merge_draft(sig)
        spec = get_spec(sig.symbol, self.spec_overrides)
        issues += validate(sig, spec, market_price, independent, self.cfg)
        issues = _dedupe_issues(issues)
        decision = route(sig, issues, self.cfg)

        mopen = self.market.market_open(sig.symbol)
        if not mopen and decision == "publish":
            if self.closed_policy == "review":
                issues.append(Issue("MARKET_CLOSED", REVIEW, "بازار بسته است"))
                decision = "review"
            elif self.closed_policy == "block":
                decision = "reject"
        if self.stale_stream and decision == "publish":
            issues.append(Issue("STALE_STREAM", REVIEW, "قیمت نمودار با بازار نمی‌خواند (ضبط قدیمی؟)"))
            decision = "review"

        result = self.trades.handle_entry(sig, issues, decision, mopen)
        if result == "published":
            self.last_signal_at = clock.now()
            if sig.spoken_at:
                self.latencies.append(clock.now() - sig.spoken_at)
        if result in {"published", "updated", "review"}:
            self._record_latency(sig, result)
        self.recorder.write("decision", intent=sig.intent, result=result, decision=decision,
                            signal=sig.to_dict(), issues=[str(i) for i in issues],
                            market={"price": market_price, "src": msrc})
        self.log.info("رویداد %s %s %s → %s (%s)", sig.intent, sig.symbol, sig.side, result,
                      ", ".join(i.code for i in issues) or "بدون مشکل")
        return result

    def _safety_issues(self, sig, ev: dict[str, Any], frame: dict[str, Any]) -> list[Issue]:
        """تور ایمنی مستقل از قضاوت مدل."""
        issues: list[Issue] = []
        evidence = sig.evidence or ""
        flags = speech_flags(evidence) if evidence else set()
        if sig.intent == "ACTUAL_ENTRY":
            if "NEGATION" in flags:
                issues.append(Issue("SPEECH_NEGATION", REVIEW, "در جمله‌ی شاهد نشانه‌ی نفی هست"))
            if "PAST" in flags:
                issues.append(Issue("SPEECH_PAST", REVIEW, "جمله‌ی شاهد به گذشته اشاره دارد"))
            if "HEDGE" in flags:
                issues.append(Issue("SPEECH_HEDGE", REVIEW, "جمله‌ی شاهد حالت فرضی/شرطی دارد"))
        if not evidence.strip():
            if sig.is_new_drawing is False:
                issues.append(Issue("OLD_DRAWING_ONLY", "fatal", "فقط رسم قدیمی، بدون گفتار"))
            else:
                issues.append(Issue("NO_SPEECH_EVIDENCE", REVIEW, "سیگنال فقط از تصویر، بدون تأیید گفتاری"))
        if frame.get("multi_chart"):
            issues.append(Issue("MULTI_CHART", REVIEW, "چند نمودار هم‌زمان روی صفحه"))
        if frame.get("obstructed"):
            issues.append(Issue("OBSTRUCTED", REVIEW, "بخشی از نمودار پوشیده است"))
        if sig.conflict:
            issues.append(Issue("SPEECH_CHART_CONFLICT", REVIEW, "عدد گفتار و تصویر متناقض است"))
        if sig.speaker_is_analyst is False:
            issues.append(Issue("NOT_ANALYST", REVIEW, "گوینده تحلیلگر اصلی نیست"))
        return issues

    def _find_chunk(self, evidence: str) -> tuple[float, float] | None:
        if not evidence:
            return None
        words = [w for w in normalize_trading_text(evidence).split() if len(w) > 1][:6]
        for t0, t1, txt in reversed(self._transcript):
            if words and sum(w in txt for w in words) >= max(1, len(words) // 2):
                return t0, t1
        return None

    def _find_spoken_time(self, evidence: str) -> float | None:
        c = self._find_chunk(evidence)
        return c[0] if c else None

    def _record_latency(self, sig, result: str) -> None:
        now = time.time()
        vs, ve = self._vision_window or (now, now)
        chunk = self._find_chunk(sig.evidence)
        audio = self._audio_timing.get(chunk) if chunk else None
        stages = compute_stages(audio, vs, ve, now)
        trade = self.trades.last_trade
        msg_id = None
        if result in {"published", "updated"} and trade is not None:
            msg_id = trade.last_msg_id or trade.msg_id
        rec = self.latency.add(trade.id if trade else None, result, stages, msg_id=msg_id, enqueued_at=now)
        parts = ", ".join(f"{STAGE_FA[k]}={v:.1f}s" for k, v in rec["stages"].items() if v is not None)
        self.log.info("⏱ تأخیر %s (%s): %s", trade.id if trade else "?", result, parts)

    # ---------- control ----------
    def start(self) -> None:
        self._running = True
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._vision_task:
            self._vision_task.cancel()
            self._vision_task = None
        self._pending_chart = False
        self.trades.save()

    def stats(self) -> dict[str, Any]:
        lat = sorted(self.latencies)
        return {
            "chart_events": self.chart_events,
            "speech_triggers": self.speech_triggers,
            "vision_calls": self.vision_calls,
            "vision_errors": self.vision_errors,
            "prefilter_skips": self.prefilter_skips,
            "vision_busy": not self._vision_idle(),
            "pending_chart": self._pending_chart,
            "symbol": self.last_symbol,
            "timeframe": self.last_timeframe,
            "frame": (self._last_info.kind if self._last_info and self._last_info.valid
                      else (self._last_info.reason if self._last_info else None)),
            "stale_stream": self.stale_stream,
            "latency_median_sec": round(lat[len(lat) // 2], 1) if lat else None,
            "latency_stages": self.latency.summary(),
            "latency_biggest": self.latency.biggest_stage(),
            "trades": self.trades.summary(),
            "outbox": self.pub.pending_count(),
            "telegram_online": self.pub.online,
        }


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    seen, out = set(), []
    for i in issues:
        key = (i.code, i.message)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


__all__ = ["ChartWatcher", "clip_region_to_monitor", "fmt_price", "monitor_for_region"]
