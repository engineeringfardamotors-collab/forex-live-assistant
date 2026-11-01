"""ضبط صفحه (چارت) و صدای سیستم (گفتار تحلیلگر) در حین جلسه.

- اسکرین‌شات دوره‌ای با mss
- ضبط صدای سیستم با WASAPI Loopback (کتابخانه soundcard) و
  در صورت نبود آن، بازگشت به sounddevice
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from app.utils import get_logger, now_iso

# هشدارهای بی‌خطر بافر soundcard هنگام ضبط طولانی
warnings.filterwarnings("ignore", message="data discontinuity in recording")


class ScreenCapture:
    """گرفتن دوره‌ای اسکرین‌شات از پنجره/کل صفحه."""

    def __init__(self, cfg: dict[str, Any], out_dir: Path):
        cap = cfg["capture"]
        self.interval = cap["screenshot_interval_sec"]
        self.max_frames = cap["max_frames_per_analysis"]
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = get_logger("screen", cfg["logging"]["level"], cfg["logging"]["dir"])
        self._running = False
        self._task: asyncio.Task | None = None
        self.saved_count = 0

    async def _loop(self) -> None:
        import mss  # import داخلی تا در سیستم‌های بدون نمایشگر خطا ندهد

        while self._running:
            try:
                with mss.mss() as sct:
                    shot = sct.grab(sct.monitors[1])
                    from PIL import Image

                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    ts = now_iso().replace(":", "-").replace("T", "_")
                    path = self.out_dir / f"frame_{ts}.png"
                    # کدگذاری PNG جلوی event loop بلاک نشود
                    await asyncio.to_thread(img.save, path, "PNG")
                    self.saved_count += 1
                    self.log.debug("فریم ذخیره شد: %s", path.name)
            except Exception as e:  # noqa: BLE001
                self.log.warning("خطا در گرفتن اسکرین‌شات: %s", e)
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._running = True
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    def latest_frames(self, n: int | None = None) -> list[Path]:
        n = n or self.max_frames
        frames = sorted(self.out_dir.glob("frame_*.png"))
        return frames[-n:]


class AudioCapture:
    """ضبط صدای سیستم در قطعات WAV.

    ترتیب اولویت:
    1) soundcard + WASAPI Loopback  (بهترین حالت؛ نیازی به نصب درایور اضافه ندارد)
    2) sounddevice با دستگاه ورودی (میکروفون/استریو میکس)
    """

    def __init__(self, cfg: dict[str, Any], out_dir: Path, on_chunk: Callable[[Path], Awaitable[None]]):
        cap = cfg["capture"]
        self.sample_rate = int(cap["audio_sample_rate"])
        self.chunk_sec = int(cap["audio_chunk_sec"])
        self.overlap_sec = float(cap.get("audio_overlap_sec", 0))
        self.silence_peak = float(cap.get("silence_peak", 1e-3))
        self.chunk_times: dict[str, tuple[float, float]] = {}
        self.chunk_levels: dict[str, float] = {}   # سطح هر قطعه؛ تصمیم سکوت باید از سطح «همان» قطعه باشد
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.on_chunk = on_chunk
        self.log = get_logger("audio", cfg["logging"]["level"], cfg["logging"]["dir"])
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream = None
        self._recorder = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.backend = "none"
        self.device_name: str | None = None    # انتخاب کاربر: نام دستگاه (loopback یا ورودی)
        self.device_index: int | None = None   # انتخاب کاربر: ایندکس دستگاه ورودی sounddevice
        self.last_level: float = 0.0
        self.chunks_written = 0
        self.signal_chunks = 0
        self.last_signal_at: float = 0.0

    # ---------- device discovery ----------
    @staticmethod
    def list_loopback_devices() -> list[str]:
        try:
            import soundcard as sc

            return [m.name for m in sc.all_microphones(include_loopback=True) if m.isloopback]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def list_sounddevice_inputs() -> list[str]:
        try:
            import sounddevice as sd

            return [d["name"] for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        except Exception:  # noqa: BLE001
            return []

    # ---------- shared flush ----------
    def _flush(self, audio: np.ndarray) -> None:
        ts = now_iso().replace(":", "-").replace("T", "_")
        path = self.out_dir / f"audio_{ts}.wav"
        sf.write(path, audio, self.sample_rate)
        t1 = time.time()
        self.chunk_times[path.name] = (t1 - audio.shape[0] / self.sample_rate, t1)
        self.chunks_written += 1
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        self.chunk_levels[path.name] = peak
        if len(self.chunk_times) > 500:
            self.chunk_times = dict(list(self.chunk_times.items())[-200:])
            self.chunk_levels = {k: v for k, v in self.chunk_levels.items() if k in self.chunk_times}
        self.last_level = peak
        if peak >= self.silence_peak:
            self.signal_chunks += 1
            self.last_signal_at = time.time()
        if peak < 1e-4:
            self.log.warning("قطعه صوتی تقریباً بی‌صدا بود (شاید صدای جلسه پخش نمی‌شود)")
        else:
            self.log.info("قطعه صوتی ذخیره شد (سطح: %.3f)", peak)
        try:
            fut = self.on_chunk(path)
            if asyncio.iscoroutine(fut):
                self._dispatch(fut)
        except Exception as e:  # noqa: BLE001
            self.log.warning("خطا در ارسال قطعه صوتی: %s", e)

    def _dispatch(self, coro: Awaitable) -> None:
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            # جلوگیری از هشدار "coroutine was never awaited"
            try:
                coro.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    # ---------- workers ----------
    def _worker_queue(self) -> None:
        """مسیر پشتیبان sounddevice: مانند _worker_soundcard همپوشانی و زمان‌بندی قطعه دارد."""
        buf: list[np.ndarray] = []
        total = 0
        need = self.sample_rate * self.chunk_sec
        while self._running or not self._q.empty():
            try:
                chunk = self._q.get(timeout=1.0)
            except queue.Empty:
                if not self._running:
                    break
                continue
            buf.append(chunk)
            total += chunk.shape[0]
            if total >= need:
                joined = np.concatenate(buf)
                audio = joined[:need]
                # همپوشانی: انتهای این قطعه ابتدای قطعه‌ی بعد هم هست تا جمله‌ها نصفه نشوند
                keep = int(self.sample_rate * self.overlap_sec)
                rest = joined[need - keep:] if keep > 0 else joined[need:]
                buf, total = ([rest], rest.shape[0]) if rest.size else ([], 0)
                self._flush(audio)

    def _worker_soundcard(self) -> None:
        block = max(1, int(self.sample_rate * 0.25))
        buf: list[np.ndarray] = []
        total = 0
        need = self.sample_rate * self.chunk_sec
        while self._running:
            try:
                data = self._recorder.record(numframes=block)
            except Exception as e:  # noqa: BLE001
                if self._running:
                    self.log.warning("خطا در خواندن loopback: %s", e)
                break
            mono = data.mean(axis=1) if getattr(data, "ndim", 1) > 1 else data
            buf.append(np.asarray(mono, dtype=np.float32))
            total += mono.shape[0]
            if total >= need:
                joined = np.concatenate(buf)
                audio = joined[:need]
                # همپوشانی: انتهای این قطعه ابتدای قطعه‌ی بعد هم هست تا جمله‌ها نصفه نشوند
                keep = int(self.sample_rate * self.overlap_sec)
                rest = joined[need - keep:] if keep > 0 else joined[need:]
                buf, total = ([rest], rest.shape[0]) if rest.size else ([], 0)
                self._flush(audio)

    # ---------- legacy sounddevice callback ----------
    def _sd_callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if status:
            self.log.debug("audio status: %s", status)
        self._q.put(indata.copy())

    # ---------- control ----------
    def _start_soundcard(self) -> bool:
        try:
            import soundcard as sc
        except ImportError:
            self.log.info("soundcard نصب نیست؛ به sounddevice بازمی‌گردم")
            return False
        try:
            mics = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
            if not mics:
                self.log.warning("هیچ دستگاه Loopback پیدا نشد")
                return False
            # انتخاب بلندگوی پیش‌فرض اگر ممکن باشد
            target = mics[0]
            if self.device_name:
                wanted = self.device_name.lower()
                matches = [m for m in mics if wanted in m.name.lower()]
                if matches:
                    target = matches[0]
                    self.log.info("دستگاه انتخابی کاربر: %s", target.name)
                else:
                    self.log.warning("دستگاه «%s» بین دستگاه‌های Loopback پیدا نشد؛ بلندگوی پیش‌فرض استفاده می‌شود", self.device_name)
            else:
                try:
                    default_spk = sc.default_speaker()
                    for m in mics:
                        if default_spk and default_spk.name in m.name:
                            target = m
                            break
                except Exception:  # noqa: BLE001
                    pass

            channels = 2
            try:
                self._recorder = target.recorder(samplerate=self.sample_rate, channels=channels)
                self._recorder.__enter__()
            except Exception:  # noqa: BLE001
                # برخی کارت‌ها نرخ 16k را نمی‌پذیرند
                self.sample_rate = 48000
                self._recorder = target.recorder(samplerate=self.sample_rate, channels=channels)
                self._recorder.__enter__()

            self.backend = f"soundcard-loopback ({target.name})"
            self._thread = threading.Thread(target=self._worker_soundcard, daemon=True)
            self._thread.start()
            self.log.info("ضبط صدای سیستم فعال شد: %s @ %d Hz", target.name, self.sample_rate)
            return True
        except Exception as e:  # noqa: BLE001
            self.log.warning("راه‌اندازی soundcard ناموفق بود: %s", e)
            return False

    def _resolve_sd_device(self) -> int | None:
        """ایندکس دستگاه ورودی sounddevice از روی نام انتخابی کاربر."""
        if not self.device_name:
            return None
        try:
            import sounddevice as sd

            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0 and self.device_name.lower() in str(d.get("name", "")).lower():
                    return i
        except Exception:  # noqa: BLE001
            pass
        self.log.warning("دستگاه ورودی «%s» پیدا نشد؛ دستگاه پیش‌فرض استفاده می‌شود", self.device_name)
        return None

    def _start_sounddevice(self, device: int | None) -> bool:
        try:
            import sounddevice as sd
        except ImportError:
            self.log.error("sounddevice نصب نیست")
            return False
        if device is None:
            device = self._resolve_sd_device()
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=self._sd_callback,
                device=device,
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            self.log.error("راه‌اندازی sounddevice ناموفق بود: %s", e)
            return False
        self.backend = "sounddevice"
        self._thread = threading.Thread(target=self._worker_queue, daemon=True)
        self._thread.start()
        self.log.info("ضبط صدا با sounddevice فعال شد (نرخ %d Hz)", self.sample_rate)
        return True

    def start(self, device: int | str | None = None) -> None:
        # انتخاب کاربر: نام دستگاه (str) یا ایندکس دستگاه ورودی (int)؛ None = خودکار
        if isinstance(device, str):
            self.device_name = device.strip() or None
        elif device is not None:
            try:
                self.device_index = int(device)
            except (TypeError, ValueError):
                self.device_name = str(device)
        self._running = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        if self.device_index is None and self._start_soundcard():
            return
        if not self._start_sounddevice(self.device_index):
            self.log.error("هیچ راهی برای ضبط صدا پیدا نشد؛ فقط تصویر ضبط می‌شود")

    def stop(self) -> None:
        # ابتدا پرچم توقف، سپس انتظار برای پایان thread و در آخر بستن دستگاه
        self._running = False
        if self._thread:
            self._thread.join(timeout=4)
            self._thread = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        if self._recorder is not None:
            try:
                self._recorder.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._recorder = None
