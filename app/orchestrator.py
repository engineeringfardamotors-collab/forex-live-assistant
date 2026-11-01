"""هماهنگ‌کننده‌ی اصلی جلسه: از ورود تا تحلیل و ارسال سیگنال.

جریان کار:
1. شنود گروه تلگرام → لینک جلسه → مرورگر باز می‌شود.
2. ضبط صدا (قطعات همپوشان) و پایش زنده‌ی صفحه آغاز می‌شود.
3. ChartWatcher رویدادهای معاملاتی را لحظه‌ای استخراج و پس از اعتبارسنجی منتشر می‌کند.
4. خلاصه‌ی دوره‌ای (اختیاری) هر چند دقیقه.
5. پایان جلسه: صفحه‌ی «جلسه تمام شد»، بسته‌شدن مرورگر، نبود گفتار/تصویر معتبر، یا سقف زمان.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.ai import AIProvider, format_signal_message
from app.browser_bot import MeetingJoiner
from app.capturers import AudioCapture, ScreenCapture
from app.config import get_config
from app.health import CaptureHealth
from app.live_watcher import ChartWatcher
from app.market import MarketPriceProvider
from app.publisher import TelegramPublisher
from app.recorder import SessionRecorder
from app.trade_state import TradeBook
from app.utils import get_logger


class SessionOrchestrator:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or get_config()
        self.log = get_logger("orchestrator", self.cfg["logging"]["level"], self.cfg["logging"]["dir"])
        self.data_dir = Path("session_data")
        self.ai = AIProvider(self.cfg)
        self.publisher = TelegramPublisher(self.cfg, self.data_dir)
        self.recorder = SessionRecorder(self.cfg, self.data_dir / "sessions")
        self.market = MarketPriceProvider(self.cfg)
        self.trades = TradeBook(self.cfg, self.publisher,
                                state_file=self.data_dir / "trades.json")
        self.trades.load()   # بازیابی معامله‌های زنده پس از ری‌استارت (کرش/شروع مجدد)
        self.publisher.on_callback = self.trades.on_callback
        # هرس نگاشت sent پیام معامله‌های زنده را حذف نکند
        self.publisher.protect_ids = self.trades.protected_msg_ids
        self.transcript: deque[str] = deque(maxlen=200)
        self.chart_analyses: deque[str] = deque(maxlen=20)
        self.screen = ScreenCapture(self.cfg, self.data_dir / "frames")
        self.audio_device = self.cfg["capture"].get("audio_device") or None
        self.audio = AudioCapture(self.cfg, self.data_dir / "audio", self._on_audio_chunk)
        self.joiner = MeetingJoiner(self.cfg)
        self.watcher = ChartWatcher(self.cfg, self.ai, self.publisher, self.data_dir / "live_frames",
                                    recorder=self.recorder, market=self.market, trades=self.trades)
        self.health = CaptureHealth(self.cfg, frame_info=lambda: self.watcher._last_info,
                                    notify=self.publisher.notify_admin)
        self.in_meeting = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.active_url: str = ""
        self.joined: bool = False
        self.started_at: float = 0.0
        self.last_signal: dict[str, Any] | None = None
        self.signal_count: int = 0
        self.end_reason: str = ""
        self._reconnects: int = 0
        self._stable_since: float = 0.0
        self._analysis_task: asyncio.Task | None = None
        self._join_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._stt_queue: asyncio.Queue = asyncio.Queue()
        self._stt_task: asyncio.Task | None = None
        self._stt_running = False

    # ---------- callbacks ----------
    async def _on_audio_chunk(self, wav_path: Path) -> None:
        """قطعه‌ی آماده را فقط صف می‌کند؛ تبدیل گفتار به‌ترتیب قطعه‌ها در _stt_worker انجام می‌شود."""
        t0, t1 = self.audio.chunk_times.get(wav_path.name, (time.time() - self.audio.chunk_sec, time.time()))
        # سطح «همان» قطعه (نه آخرین سطح کل ضبط): وگرنه قطعه‌ی پرصدا بعد از یک قطعه‌ی بی‌صدا حذف می‌شد.
        # سطح نامعلوم (مثلاً قطعه‌ی در جریان هنگام تعویض دستگاه) → سخاوتمندانه به STT می‌رود.
        level = self.audio.chunk_levels.get(wav_path.name)
        if level is None:
            level = 1.0
        await self._stt_queue.put((wav_path, t0, t1, float(level)))

    async def _stt_worker(self) -> None:
        """تبدیل گفتار به متن، قطعه‌به‌قطعه و **به ترتیب تولید**.

        ترتیب حیاتی است: متن هر قطعه به متن قطعه‌ی قبلی می‌چسبد (حذف همپوشانی) و
        پنجره‌ی گفتارِ مدل بینایی باید قدیمی→جدید باشد؛ اگر دو قطعه هم‌زمان تبدیل می‌شدند،
        یک تأخیر یا تلاش مجدد کافی بود تا ترتیب و ادغام به‌هم بریزد.
        """
        while self._stt_running or not self._stt_queue.empty():
            try:
                wav_path, t0, t1, level = await asyncio.wait_for(self._stt_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                kept = self.recorder.keep_audio_file(wav_path)
                if level < self.audio.silence_peak:
                    continue  # قطعه‌ی بی‌صدا: هزینه‌ی تبدیل گفتار نده
                stt_start = time.time()
                text = await self.ai.transcribe(wav_path)
                stt_end = time.time()
                if text:
                    self.transcript.append(text)
                    self.watcher.add_transcript(text, t0, t1, kept, stt_start=stt_start, stt_end=stt_end)
            except Exception as e:  # noqa: BLE001
                self.log.warning("خطا در تبدیل گفتار به متن (%s): %s", wav_path.name, e)
            finally:
                wav_path.unlink(missing_ok=True)

    def _start_stt_worker(self) -> None:
        if self._stt_task is None or self._stt_task.done():
            self._stt_running = True
            self._stt_task = asyncio.get_running_loop().create_task(self._stt_worker())

    async def _stop_stt_worker(self) -> None:
        """تا ۳ ثانیه برای خالی‌شدن صف گفتار صبر می‌کند، بعد هر چه مانده را قطع و پاک می‌کند."""
        self._stt_running = False
        task = self._stt_task
        self._stt_task = None
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=3)
            except Exception:  # noqa: BLE001  (TimeoutError یعنی wait_for خودش قطع کرده است)
                task.cancel()
        while not self._stt_queue.empty():
            try:
                wav_path, _, _, _ = self._stt_queue.get_nowait()
                wav_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                break

    async def _analysis_loop(self) -> None:
        """خلاصه‌ی دوره‌ای (سیگنال‌های لحظه‌ای را ChartWatcher می‌فرستد)."""
        minutes = float(self.cfg["analysis"].get("analysis_interval_min", 0) or 0)
        if minutes <= 0:
            return
        min_chars = self.cfg["analysis"]["min_transcript_chars"]
        while self.in_meeting:
            await asyncio.sleep(minutes * 60)
            try:
                full_text = " ".join(self.transcript)
                if len(full_text) < min_chars:
                    continue
                result = await self.ai.generate_analysis(full_text, [])
                # خلاصه هرگز سیگنال معاملاتی منتشر نمی‌کند؛ سیگنال فقط از مسیر اعتبارسنجی‌شده
                result["signal"] = None
                if self.cfg["analysis"].get("publish_summaries", True):
                    self.publisher.enqueue("📋 خلاصه‌ی بخش اخیر جلسه\n" + format_signal_message(result))
                    self.signal_count += 1
                self.last_signal = result
                self.transcript.clear()
            except Exception as e:  # noqa: BLE001
                self.log.error("خطا در چرخه‌ی خلاصه: %s", e)

    # ---------- control ----------
    async def on_meeting_link(self, url: str) -> None:
        await self.start_manual(url)

    async def start_manual(self, url: str) -> str:
        if self.in_meeting:
            return "هم‌اکنون جلسه‌ای در حال ضبط است"
        if not url:
            return "URL خالی است"
        self.log.info("شروع جلسه: %s", url)
        loop = asyncio.get_running_loop()
        self.publisher.start()
        self.in_meeting = True
        self.active_url = url
        self.joined = False
        self.started_at = time.time()
        self.end_reason = ""
        rec_dir = self.recorder.start({"url": url})
        if rec_dir:
            self.log.info("ضبط کامل جلسه در %s", rec_dir)
        self.audio.start(self.audio_device)
        self._start_stt_worker()
        self.watcher.start()
        self.health.start()
        self._analysis_task = loop.create_task(self._analysis_loop())
        self._idle_task = loop.create_task(self._end_watchdog())
        self.publisher.notify_admin(f"▶️ جلسه شروع شد: {url}")

        if self.cfg["meeting"].get("use_browser", True):
            self._join_task = loop.create_task(self._join_and_watch(url))
            return "ضبط شروع شد. مرورگر باز می‌شود؛ اگر لاگین لازم بود در همان پنجره وارد شوید."
        return "ضبط شروع شد (بدون باز کردن مرورگر)."

    async def _join_and_watch(self, url: str) -> None:
        try:
            ok = await self.joiner.join(url)
            if not ok:
                ok = await self.joiner.wait_for_manual_join()
            self.joined = bool(ok)
        except Exception as e:  # noqa: BLE001
            self.log.warning("خطا در فرآیند ورود: %s", e)

    async def _try_reconnect(self, why: str) -> bool:
        """ورود مجدد به جلسه پس از قطعی (بخش ۲۲-۶). True یعنی جلسه دوباره وصل شد و ضبط ادامه دارد."""
        r = self.cfg["meeting"].get("reconnect") or {}
        if not r.get("enabled", True) or not self.active_url:
            return False
        max_attempts = int(r.get("max_attempts", 3))
        if self._reconnects >= max_attempts:
            return False
        self._reconnects += 1
        wait = float(r.get("wait_sec", 20))
        self.log.warning("قطع جلسه (%s)؛ تلاش ورود مجدد %d/%d", why, self._reconnects, max_attempts)
        self.publisher.notify_admin(f"🔌 قطع جلسه ({why})؛ ورود مجدد {self._reconnects}/{max_attempts} …")
        await self.joiner.close()
        await asyncio.sleep(min(max(wait, 5), 90))
        try:
            ok = await self.joiner.join(self.active_url)
            if not ok:
                # برای ورود دستی فقط کمی صبر می‌کنیم، نه manual_wait_sec کامل
                ok = await self.joiner.wait_for_manual_join(timeout_sec=int(r.get("manual_join_wait_sec", 45)))
        except Exception as e:  # noqa: BLE001
            self.log.warning("ورود مجدد ناموفق: %s", e)
            ok = False
        if ok:
            self.joined = True
            self._stable_since = 0.0
            self.publisher.notify_admin("✔ ورود مجدد انجام شد؛ ضبط ادامه دارد.")
            self.log.info("ورود مجدد موفق؛ ضبط ادامه دارد")
        return bool(ok)

    async def _end_watchdog(self) -> None:
        """تشخیص پایان جلسه با چند نشانه‌ی مستقل.

        - صفحه‌ی مرورگر «جلسه تمام شد» یا بسته شدن مرورگر ⇒ تلاش ورود مجدد، سپس پایان
        - نبود گفتار معنادار و نبود فریم معتبر/تغییر محلی چارت ⇒ هشدار، سپس پایان
          (صدای موسیقی/تبلیغ گفتار معنادار حساب نمی‌شود چون متنی تولید نمی‌کند)
        - سقف زمان جلسه
        """
        a = self.cfg["analysis"]
        idle_limit = float(a.get("auto_end_idle_min", 8)) * 60
        warn_at = idle_limit * 0.6
        max_sec = float(a.get("max_session_hours", 4)) * 3600
        warned = False
        browser_closed_since: float | None = None
        while self.in_meeting:
            await asyncio.sleep(15)
            now = time.time()
            state = await self.joiner.meeting_state()
            if state == "in_call":
                # جلسه پایدار: سهمیه‌ی ورود مجدد پس از ۱۰ دقیقه‌ی بی‌مشکل تازه می‌شود
                if not self._stable_since:
                    self._stable_since = now
                elif now - self._stable_since > 600:
                    self._reconnects = 0
            else:
                self._stable_since = 0.0
            if state == "ended":
                if await self._try_reconnect("صفحه‌ی پایان جلسه (احتمال قطعی)"):
                    browser_closed_since = None
                    continue
                await self.end_meeting("صفحه‌ی مرورگر پایان جلسه را نشان می‌دهد")
                return
            if state == "closed":
                browser_closed_since = browser_closed_since or now
                if now - browser_closed_since > 30:
                    if await self._try_reconnect("بسته‌شدن پنجره‌ی جلسه"):
                        browser_closed_since = None
                        continue
                    await self.end_meeting("پنجره‌ی جلسه بسته شد")
                    return
            else:
                browser_closed_since = None
            if now - self.started_at > max_sec:
                await self.end_meeting(f"سقف زمان جلسه ({max_sec / 3600:.0f} ساعت)")
                return
            last_speech = self.watcher.last_speech_at or self.started_at
            last_chart = self.watcher.last_change_at or self.started_at
            invalid = self.watcher.invalid_since is not None
            last_activity = max(last_speech, self.started_at if invalid else last_chart)
            idle = now - last_activity
            if idle > idle_limit:
                await self.end_meeting(f"{idle / 60:.0f} دقیقه بدون گفتار و تغییر نمودار")
                return
            if idle > warn_at and not warned:
                warned = True
                self.log.warning("جلسه احتمالاً تمام شده (%.0f دقیقه بی‌فعالیتی)", idle / 60)
                self.publisher.notify_admin(f"⏸ جلسه احتمالاً تمام شده ({idle / 60:.0f} دقیقه بی‌فعالیتی). "
                                            f"اگر ادامه پیدا نکند، {(idle_limit - idle) / 60:.0f} دقیقه‌ی دیگر بسته می‌شود.")
            elif idle < warn_at:
                warned = False

    # ---------- audio device ----------
    def audio_devices(self) -> dict[str, list[str]]:
        return {"loopback": AudioCapture.list_loopback_devices(),
                "inputs": AudioCapture.list_sounddevice_inputs()}

    async def set_audio_device(self, device: str | int | None) -> str:
        """تغییر دستگاه ضبط صدا (نام دستگاه یا ایندکس؛ None = خودکار)."""
        self.audio_device = device
        if not self.in_meeting:
            return "دستگاه صدا ذخیره شد؛ در جلسه‌ی بعد اعمال می‌شود"
        self.audio.stop()
        self.audio = AudioCapture(self.cfg, self.data_dir / "audio", self._on_audio_chunk)
        self.audio.start(device)
        return f"دستگاه صدا تغییر کرد ({device or 'خودکار'})؛ بک‌اند فعال: {self.audio.backend}"

    def status(self) -> dict[str, Any]:
        return {
            "in_meeting": self.in_meeting,
            "joined": self.joined or self.audio.signal_chunks > 0,
            "url": self.active_url,
            "end_reason": self.end_reason,
            "transcript_len": sum(len(t) for t in self.transcript),
            "summary_count": self.signal_count,
            "last_summary": self.last_signal,
            "audio_backend": self.audio.backend,
            "audio_device": self.audio_device,
            "audio_level": round(self.audio.last_level, 4),
            "audio_chunks": self.audio.chunks_written,
            "recording_dir": str(self.recorder.dir) if self.recorder.dir else None,
            "live": self.watcher.stats(),
            "health_warnings": self.health.warnings(),
            "market_source": self.market.status(),
            "dry_run": self.publisher.dry_run,
        }

    async def end_meeting(self, reason: str = "دستی") -> None:
        if not self.in_meeting:
            await self.joiner.close()
            await self.market.aclose()
            return
        self.log.info("پایان جلسه (%s)؛ توقف ضبط", reason)
        self.end_reason = reason
        self.in_meeting = False
        self.joined = False
        # پایان تمیز جلسه — قبل از هر await (نوشتن sync): اگر ادامه‌ی خروج قطع شود هم پرچم روی دیسک است
        # و شروع بعدی معامله‌های این جلسه را بازیابی نمی‌کند (بازیابی فقط برای کرش است)
        self.trades.close_session()
        self.audio.stop()
        await self._stop_stt_worker()
        self.watcher.stop()
        self.health.stop()
        current = asyncio.current_task()
        for task in (self._analysis_task, self._join_task, self._idle_task):
            if task and not task.done() and task is not current:
                task.cancel()
        self._analysis_task = self._join_task = self._idle_task = None
        summary = self.trades.summary()
        self.publisher.notify_admin(f"⏹ جلسه پایان یافت ({reason}). خلاصه: {summary}")
        st = self.watcher.stats()
        # آمار مدل بینایی برای سنجش بعد از جلسه‌ی واقعی (بخش ۱۶ مستندات)
        self.recorder.write("meta", end_reason=reason, trades=summary, watcher=st)
        self.log.info("آمار پایش جلسه: تحلیل بینایی=%s، محرک گفتاری=%s، رویداد چارت=%s، خطای مدل=%s",
                      st.get("vision_calls"), st.get("speech_triggers"),
                      st.get("chart_events"), st.get("vision_errors"))
        self.recorder.stop()
        await self.joiner.close()
        await self.market.aclose()
