"""تست‌های تکمیلی ۲.۲: صف ارسال، callback ادمین، MT5/HTTP ماک، همپوشانی صدا،
سلامت ضبط، قالب پیام، پله‌ی حجم، ورود مجدد و چرخش داده‌ها.

اجرا:  python tests/test_extra.py
"""
from __future__ import annotations

import asyncio
import copy
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import app.orchestrator as orch_mod  # noqa: E402
import app.market as market_mod  # noqa: E402
from app.capturers import AudioCapture  # noqa: E402
from app.config import load_config  # noqa: E402
from app.frames import FrameInfo  # noqa: E402
from app.health import CaptureHealth  # noqa: E402
from app.market import MarketPriceProvider  # noqa: E402
from app.publisher import MemoryPublisher, TelegramPublisher  # noqa: E402
from app.recorder import SessionRecorder  # noqa: E402
from app.signal_model import Signal, build_signal, BuildContext  # noqa: E402
from app.trade_state import DISCLAIMER, TradeBook  # noqa: E402
from app.winapi import WindowInfo  # noqa: E402

from test_scenarios import chart_frame, channel, ev, make_watcher, run  # noqa: E402

BASE = load_config()
CFG = copy.deepcopy(BASE)
CFG["telegram"]["admin_chat"] = "admin"
CFG["recording"] = {"enabled": False}


# ---------------- صدا: همپوشانی مسیر sounddevice ----------------
def _audio_cfg(chunk_sec: float = 10, overlap: float = 2.0) -> dict:
    cfg = copy.deepcopy(CFG)
    cfg["capture"]["audio_chunk_sec"] = chunk_sec
    cfg["capture"]["audio_overlap_sec"] = overlap
    return cfg


def test_audio_overlap_sounddevice():
    cap = AudioCapture(_audio_cfg(10, 2.0), Path(tempfile.mkdtemp()), None)  # type: ignore[arg-type]
    rate, need, keep = cap.sample_rate, cap.sample_rate * 10, int(cap.sample_rate * 2)
    flushed: list[np.ndarray] = []
    cap._flush = lambda audio: flushed.append(audio.copy())  # type: ignore[method-assign]
    rng = np.random.default_rng(7)
    stream = rng.standard_normal(3 * need).astype(np.float32)
    cap._running = True
    for i in range(0, stream.shape[0], 8000):
        cap._q.put(stream[i:i + 8000])
    cap._running = False   # پس از مصرف صف، کارگر تمام می‌شود (مانند stop())
    cap._worker_queue()
    assert len(flushed) == 3, f"باید ۳ قطعه کامل شود؛ شد {len(flushed)}"
    for f in flushed:
        assert f.shape[0] == need
    # همپوشانی: ۲ ثانیه‌ی آخر هر قطعه = ۲ ثانیه‌ی اول قطعه‌ی بعد
    assert np.allclose(flushed[1][:keep], flushed[0][need - keep:])
    assert np.allclose(flushed[2][:keep], flushed[1][need - keep:])
    # نمونه‌ها بدون گم‌شدن به ترتیب اصلی (طول هر بخش بدون همپوشانی = need - keep)
    assert np.allclose(flushed[0], stream[:need])
    assert np.allclose(flushed[1][keep:], stream[need:2 * need - keep])
    assert np.allclose(flushed[2][keep:], stream[2 * need - keep:3 * need - 2 * keep])


def test_audio_chunk_timing_and_file():
    out = Path(tempfile.mkdtemp())
    cap = AudioCapture(_audio_cfg(1, 0.0), out, None)  # type: ignore[arg-type]
    got: list[Path] = []

    def on_chunk(p: Path) -> None:
        got.append(p)

    cap.on_chunk = on_chunk  # type: ignore[method-assign]
    cap._running = True
    for _ in range(4):
        cap._q.put(np.full(4000, 0.1, dtype=np.float32))
    cap._running = False   # پس از مصرف صف، کارگر تمام می‌شود
    cap._worker_queue()
    assert len(got) == 1 and got[0].exists()
    t0, t1 = cap.chunk_times[got[0].name]
    assert 0.9 <= t1 - t0 <= 1.5, f"طول قطعه‌ی ثبت‌شده نامعتبر: {t1 - t0:.2f}s"


# ---------------- صف ارسال (outbox) ----------------
class FakeApiPublisher(TelegramPublisher):
    """همان TelegramPublisher ولی Bot API ماک: ترتیب و payload ثبت می‌شود."""

    def __init__(self, cfg: dict, state_dir: Path):
        super().__init__(cfg, state_dir)
        self.calls: list[tuple[str, dict]] = []

    async def _api(self, method: str, payload: dict, timeout: float = 30) -> dict:  # noqa: ARG002
        self.calls.append((method, payload))
        return {"message_id": 100 + len(self.calls)}


def _pub_cfg() -> dict:
    cfg = copy.deepcopy(CFG)
    cfg["telegram"]["bot_token"] = "TEST:TOKEN"
    cfg["telegram"]["signal_channel"] = "-100TEST"
    cfg["publisher"] = {"dry_run": False, "delay_note_sec": 3600, "drop_after_sec": 3600, "retry_sec": 1}
    return cfg


def test_outbox_order_and_edit():
    with tempfile.TemporaryDirectory() as td:
        pub = FakeApiPublisher(_pub_cfg(), Path(td))
        a = pub.enqueue("A")
        pub.enqueue("B", reply_to=a)
        pub.enqueue("C", edit_of=a)
        asyncio.run(pub.flush_once())
        methods = [m for m, _ in pub.calls]
        assert methods == ["sendMessage", "sendMessage", "editMessageText"], methods
        # ویرایش و Reply هر دو به پیام اصلی (message_id=101) اشاره می‌کنند
        assert pub.calls[1][1]["reply_parameters"]["message_id"] == 101
        assert pub.calls[2][1]["message_id"] == 101
        assert pub.pending_count() == 0


def test_outbox_drop_stale_and_admin_note():
    with tempfile.TemporaryDirectory() as td:
        cfg = _pub_cfg()
        cfg["publisher"]["drop_after_sec"] = 100
        pub = FakeApiPublisher(cfg, Path(td))
        old_id = pub.enqueue("خیلی قدیمی", created_at=time.time() - 9999)
        pub.enqueue("تازه")
        asyncio.run(pub.flush_once())
        asyncio.run(pub.flush_once())   # فلاش دوم: گزارش ادمین که در همان فلاش اول صف شد
        assert pub.message_id(old_id) is None   # کهنه ارسال نشد
        texts = [p["text"] for m, p in pub.calls if m == "sendMessage"]
        assert any("کهنه" in t for t in texts)  # گزارش به ادمین
        assert any(t == "تازه" for t in texts)


def test_outbox_persist_across_restart():
    with tempfile.TemporaryDirectory() as td:
        pub1 = FakeApiPublisher(_pub_cfg(), Path(td))
        pub1.enqueue("ماندگار")
        pub2 = FakeApiPublisher(_pub_cfg(), Path(td))
        assert pub2.pending_count() == 1
        asyncio.run(pub2.flush_once())
        assert pub2.pending_count() == 0


# ---------------- دکمه‌های تأیید ادمین ----------------
def _complete_signal(**kw) -> Signal:
    base = dict(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980.0, sl=3972.0,
                tps=[3995.0, 4005.0], confidence=0.9, evidence="بای لیمیت ۳۹۸۰")
    base.update(kw)
    return Signal(**base)


def test_admin_callback_approve_reject():
    cfg = copy.deepcopy(CFG)
    cfg["validation"] = {"review_ttl_sec": 300, "review_fallback": "hold"}
    pub = MemoryPublisher(cfg)
    tb = TradeBook(cfg, pub)
    tb.handle_entry(_complete_signal(), [], "review", True)
    t = tb.trades["T1"]
    assert t.status == "REVIEW"
    answer = asyncio.run(tb.on_callback("approve", "T1"))
    assert answer == "ارسال شد ✅" and t.status == "SENT"
    assert channel(pub) and "سیگنال XAUUSD" in channel(pub)[0]["text"]

    tb.handle_entry(_complete_signal(entry=3960.0), [], "review", True)
    t2 = tb.trades["T2"]
    answer2 = asyncio.run(tb.on_callback("reject", "T2"))
    assert answer2 == "رد شد ❌" and t2.status == "REJECTED"
    assert asyncio.run(tb.on_callback("approve", "T99")) == "پیدا نشد"


# ---------------- بازار: MT5 ماک و پیش‌تنظیم HTTP ----------------
class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeMT5:
    def initialize(self, path=None, **kw):  # noqa: ARG002
        return True

    def terminal_info(self):
        return _Obj(company="FakeBroker")

    def account_info(self):
        return _Obj(server="Fake-Server")

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, name):
        return _Obj(name=name) if name == "XAUUSDm" else None

    def symbols_get(self):
        return [_Obj(name="XAUUSDm")]

    def symbol_select(self, name, enable=True):  # noqa: ARG002
        return True

    def symbol_info_tick(self, name):  # noqa: ARG002
        return _Obj(bid=3990.0, ask=3990.4, time=time.time())


def test_market_mt5_mock():
    sys.modules["MetaTrader5"] = FakeMT5()
    try:
        cfg = copy.deepcopy(CFG)
        cfg["market"] = {"sources": ["mt5"], "mt5_max_tick_age_sec": 300}
        p = MarketPriceProvider(cfg)
        price = p._mt5_price("XAUUSD")   # متد سنکرون (در get_price با to_thread صدا زده می‌شود)
        assert price == 3990.2, price
        assert p.mt5_state.startswith("وصل")
        assert p._resolved.get("XAUUSD") == "XAUUSDm"
        # تیک کهنه در بازار باز → قیمت مستقل داده نمی‌شود (آستانه‌ی داخلی: max_tick_age + 12h)
        p.market_open = lambda s, now=None: True  # type: ignore[method-assign]
        p._mt5.symbol_info_tick = lambda name: _Obj(bid=3990.0, ask=3990.4, time=time.time() - 24 * 3600)
        stale = p._mt5_price("XAUUSD")
        assert stale is None and "کهنه" in p.mt5_state
    finally:
        sys.modules.pop("MetaTrader5", None)


def _mock_http(payload: dict) -> dict:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    real = httpx.AsyncClient

    def fake_client(**kw):  # noqa: ARG001
        return real(transport=httpx.MockTransport(handler), timeout=6)

    return seen, fake_client


def test_market_http_preset():
    seen, fake_client = _mock_http({"name": "Gold", "price": 3991.5})
    with patched(market_mod.httpx, "AsyncClient", fake_client):
        cfg = copy.deepcopy(CFG)
        cfg["market"] = {"sources": ["http"], "http": {"preset": "gold-api"}}
        p = MarketPriceProvider(cfg)
        price = asyncio.run(p._http_price("XAUUSD"))
        assert price == 3991.5
        assert seen["url"].endswith("/XAU")
        # get_price: منبع http مستقل است و کش می‌شود
        price2, src, independent = asyncio.run(p.get_price("XAUUSD"))
        assert (price2, src, independent) == (3991.5, "http", True)
        assert p.status()["http"]["enabled"] is True


def test_market_http_preset_user_map():
    seen, fake_client = _mock_http({"price": 49.3})
    with patched(market_mod.httpx, "AsyncClient", fake_client):
        cfg = copy.deepcopy(CFG)
        cfg["market"] = {"sources": ["http"], "http": {"preset": "gold-api", "symbol_map": {"XAUUSD": "GOLD"}}}
        p = MarketPriceProvider(cfg)
        assert asyncio.run(p._http_price("XAUUSD")) == 49.3
        assert seen["url"].endswith("/GOLD")
        # نماد بدون نگاشت از نقشه‌ی پیش‌فرض استفاده می‌کند
        asyncio.run(p._http_price("XAGUSD"))
        assert seen["url"].endswith("/XAG")


# ---------------- سلامت ضبط (پنجره‌ی ماک) ----------------
def _win(rect=(100, 100, 1100, 800), minimized=False, hwnd=1) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, title="جلسه", rect=rect, minimized=minimized, maximized=False, pid=1)


@contextmanager
def patched(obj: Any, name: str, value: Any) -> Iterator[None]:
    """جایگزین موقت یک ویژگی (بدون pytest fixture)."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def test_health_window_warnings():
    h = CaptureHealth(CFG)
    h.check_window(None)
    assert "WINDOW_MISSING" in h.active
    h.check_window(_win())
    assert "WINDOW_MISSING" not in h.active
    h.check_window(_win(minimized=True))
    assert "WINDOW_MINIMIZED" in h.active

    h2 = CaptureHealth(CFG)
    h2.check_window(_win())
    h2.check_window(_win(rect=(260, 100, 1260, 800)))
    assert "WINDOW_MOVED" in h2.active   # جابه‌جایی ۱۶۰px > تلورانس ۸
    h2.check_window(_win(rect=(104, 100, 1104, 800)))
    assert "WINDOW_MOVED" not in h2.active   # داخل تلورانس


def test_health_frame_black_and_frozen():
    h = CaptureHealth(CFG)
    t0 = time.time() - 100
    h.check_frame(FrameInfo(valid=False, reason="blank"), now=t0)
    assert "FRAME_BLACK" not in h.active   # هنوز زیر آستانه
    h.check_frame(FrameInfo(valid=False, reason="blank"), now=t0 + 35)
    assert "FRAME_BLACK" in h.active
    h.check_frame(FrameInfo(valid=True, reason="", frozen_sec=40.0), now=t0 + 40)
    assert "FRAME_FROZEN" in h.active


# ---------------- قالب دلخواه پیام ----------------
def test_signal_template_custom():
    cfg = copy.deepcopy(CFG)
    cfg["signal"] = {"template": "⚡ {symbol} — {side} {order_type}\n🎯 {entry}\n🛑 {sl}\n💰 {tps}\n🆔 {id}"}
    pub = MemoryPublisher(cfg)
    tb = TradeBook(cfg, pub)
    msg = tb.format_signal(_complete_signal(), "T9")
    assert msg.startswith("⚡ XAUUSD — 🟢 خرید")
    assert "🎯 3980" in msg and "🛑 3972" in msg and "TP1: 3995" in msg
    assert DISCLAIMER in msg and "اطمینان استخراج" not in msg   # پیام پیش‌فرض جایگزین شده

    tb2 = TradeBook(CFG, MemoryPublisher(CFG))
    default_msg = tb2.format_signal(_complete_signal(), "T9")
    assert default_msg.startswith("⚡ سیگنال XAUUSD") and "اطمینان استخراج" in default_msg


# ---------------- پله‌ی حجم: REENTRY و ADD_ENTRY ----------------
def test_reentry_with_portion():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev())
    res = asyncio.run(w._handle_event(
        {"intent": "MANAGEMENT", "symbol": "XAUUSD", "side": "BUY",
         "management": {"action": "REENTRY", "price": 3981, "percent": 50},
         "sl": 3972, "tps": [3995], "confidence": 0.9,
         "evidence_quote": "نصف حجم دوباره وارد شید", "speaker_is_analyst": True, "is_new_drawing": True},
        chart_frame(), "XAUUSD"))
    assert res in {"updated", "published", "review"}
    entries = trades.trades["T1"].signal.entries
    assert entries and entries[0]["price"] == 3981.0 and entries[0]["portion"] == 0.5, entries


def test_add_entry_with_portion():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev())
    res = asyncio.run(w._handle_event(
        {"intent": "MANAGEMENT", "symbol": "XAUUSD",
         "management": {"action": "ADD_ENTRY", "price": 3978, "portion": 0.3},
         "evidence_quote": "سی درصد اینجا هم بگیرید", "confidence": 0.9, "speaker_is_analyst": True},
        chart_frame(), "XAUUSD"))
    assert res == "managed"
    entries = trades.trades["T1"].signal.entries
    assert entries and entries[0]["price"] == 3978.0 and entries[0]["portion"] == 0.3, entries
    texts = [m["text"] for m in pub.sent if m["target"] == "channel"]
    assert any("30٪ حجم" in t for t in texts)


# ---------------- پیش‌فیلتر ارزان ----------------
class PrecheckAI:
    def __init__(self, allow: bool):
        self.precheck_enabled = True
        self.allow = allow
        self.prechecks = 0
        self.vision_calls = 0

    async def precheck_frame(self, path):  # noqa: ARG002
        self.prechecks += 1
        return self.allow

    async def extract_live_events(self, *a, **k):  # noqa: ARG002
        self.vision_calls += 1
        return {"frame": {"type": "chart", "symbol_on_chart": "XAUUSD", "current_price": 3982.4}, "events": []}, ""


def _small_img() -> Image.Image:
    rng = np.random.default_rng(3)
    return Image.frombytes("RGB", (80, 60), rng.integers(0, 256, (60, 80, 3), dtype=np.uint8).tobytes())


def test_vision_prefilter_skips_model():
    w, _, _ = make_watcher()
    ai = PrecheckAI(allow=False)
    w.ai = ai  # type: ignore[assignment]
    from app.frames import FrameAnalyzer

    info = FrameInfo(valid=True, kind="local")
    asyncio.run(w.process(_small_img(), "chart", info))
    assert ai.prechecks == 1 and ai.vision_calls == 0 and w.prefilter_skips == 1
    # محرک گفتاری همیشه از پیش‌فیلتر رد می‌شود
    w._last_vision = 0.0   # ریست cooldown برای تست
    asyncio.run(w.process(_small_img(), "speech", info))
    assert ai.vision_calls == 1


def test_vision_prefilter_pass_calls_model():
    w, _, _ = make_watcher()
    ai = PrecheckAI(allow=True)
    w.ai = ai  # type: ignore[assignment]
    info = FrameInfo(valid=True, kind="local")
    asyncio.run(w.process(_small_img(), "chart", info))
    assert ai.prechecks == 1 and ai.vision_calls == 1 and w.prefilter_skips == 0


# ---------------- ورود مجدد (reconnect) ----------------
class StubJoiner:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.closes = 0

    async def join(self, url):  # noqa: ARG002
        return self.ok

    async def wait_for_manual_join(self, timeout_sec=None):  # noqa: ARG002
        return self.ok

    async def close(self):
        self.closes += 1

    async def meeting_state(self):
        return "in_call"


async def _nosleep(_):
    return None


def test_try_reconnect_attempts_and_disable():
    from app.orchestrator import SessionOrchestrator

    with patched(orch_mod.asyncio, "sleep", _nosleep):
        cfg = copy.deepcopy(CFG)
        cfg["meeting"]["reconnect"] = {"enabled": True, "max_attempts": 2, "wait_sec": 0}
        o = SessionOrchestrator(cfg)
        o.active_url = "https://example.zoom.us/j/123"   # بدون لینک، reconnect امتناع می‌شود
        o.joiner = StubJoiner()  # type: ignore[assignment]
        assert asyncio.run(o._try_reconnect("تست")) is True
        assert o.joined and o._reconnects == 1
        o.joiner.ok = False
        assert asyncio.run(o._try_reconnect("تست")) is False
        assert o._reconnects == 2
        assert asyncio.run(o._try_reconnect("تست")) is False   # سهمیه تمام شد

    cfg2 = copy.deepcopy(cfg)
    cfg2["meeting"]["reconnect"] = {"enabled": False}
    o2 = SessionOrchestrator(cfg2)
    o2.active_url = "https://example.zoom.us/j/123"
    o2.joiner = StubJoiner()  # type: ignore[assignment]
    assert asyncio.run(o2._try_reconnect("تست")) is False


# ---------------- چرخش داده‌ها ----------------
def test_recorder_rotation():
    root = Path(tempfile.mkdtemp())
    now = datetime.now()
    specs = {"ancient": now - timedelta(days=200), "mid": now - timedelta(days=5),
             "recent": now - timedelta(days=1)}
    for name, ts in specs.items():
        d = root / ts.strftime("%Y%m%d_%H%M%S")
        (d / "frames").mkdir(parents=True)
        (d / "events.jsonl").write_text('{"t":1}\n', encoding="utf-8")

    cfg = copy.deepcopy(CFG)
    cfg["recording"] = {"enabled": True, "keep_days": 14, "compress_after_days": 3}
    rec = SessionRecorder(cfg, root=root)
    rec.start({"url": "x"})
    names = {p.name for p in root.iterdir()}
    # پوشه‌ی قدیمی حذف و میانی به zip تبدیل شده (zip خودش هم‌نام پیشوند جلسه است)
    assert not any(n.startswith(specs["ancient"].strftime("%Y%m%d")) and (root / n).is_dir() for n in names)
    assert not any(n.startswith(specs["mid"].strftime("%Y%m%d")) and (root / n).is_dir() for n in names)
    assert sum(1 for n in names if n.endswith(".zip")) == 1, names   # میانی zip شد (خیلی قدیمی حذف شد)
    # جلسه‌ی تازه (که start ساخت) و جلسه‌ی دیروز سر جایشان هستند
    recent_prefix = specs["recent"].strftime("%Y%m%d")
    assert any(n.startswith(recent_prefix) and (root / n).is_dir() for n in names)


# ---------------- 2.3.0: ترتیب STT، سطح قطعه، مانیتور، تحلیل غیربلاک‌کننده ----------------
class _SlowSTT:
    """هوش مصنوعی ساختگی با تأخیر کنترل‌شده برای تست ترتیب و هم‌زمانی تبدیل گفتار."""

    def __init__(self, delays: list[float]):
        self.delays = list(delays)
        self.calls = 0

    async def transcribe(self, wav_path: Path) -> str:
        d = self.delays[min(self.calls, len(self.delays) - 1)]
        self.calls += 1
        await asyncio.sleep(d)
        return f"متن {wav_path.stem}"


def _make_orch() -> "orch_mod.SessionOrchestrator":
    cfg = copy.deepcopy(CFG)
    cfg["recording"] = {"enabled": False}
    cfg["telegram"]["admin_chat"] = ""
    o = orch_mod.SessionOrchestrator(cfg)
    # ناشر ایزوله روی دایرکتوری موقت — صف مانده‌ی اجراهای قبلی (session_data/outbox.json)
    # نباید در تست نفوذ کند (خود تست reconnect پیام ادمین در outbox واقعی می‌نویسد)
    pub = TelegramPublisher(cfg, Path(tempfile.mkdtemp()))
    o.publisher = pub
    o.trades.pub = pub
    o.trades.state_file = None   # تست‌ها وضعیت معاملات پروژه (session_data/trades.json) را نمی‌نویسند
    return o


def test_stt_chunks_transcribed_in_order():
    """قطعه‌های صوتی باید دقیقاً به ترتیب تولید تبدیل شوند حتی وقتی قطعه‌ی اول کُندتر است.

    ترتیب برای حذف همپوشانی (merge_overlap) و پنجره‌ی گفتارِ مدل بینایی حیاتی است.
    """
    orch = _make_orch()
    ai = _SlowSTT([0.3, 0.0, 0.0])   # قطعه‌ی اول کُند: اگر هم‌زمان تبدیل می‌شد ترتیب به‌هم می‌ریخت
    orch.ai = ai  # type: ignore[assignment]

    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        paths = []
        for i in range(3):
            p = tmp / f"audio_{i}.wav"
            p.write_bytes(b"x")
            paths.append(p)
        orch._stt_running = True
        task = asyncio.get_running_loop().create_task(orch._stt_worker())
        await asyncio.gather(*(orch._on_audio_chunk(p) for p in paths))
        await asyncio.sleep(0.8)
        orch._stt_running = False
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    texts = [t[2] for t in orch.watcher._transcript]
    assert texts == ["متن audio_0", "متن audio_1", "متن audio_2"], texts
    assert ai.calls == 3


def test_silence_gate_uses_chunk_level_not_last_level():
    """قطعه‌ی پرصدا بعد از قطعه‌ی بی‌صدا نباید حذف شود (سطح هر قطعه ملاک است، نه last_level)."""
    orch = _make_orch()
    ai = _SlowSTT([0.0, 0.0])
    orch.ai = ai  # type: ignore[assignment]

    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        silent = tmp / "audio_silent.wav"
        loud = tmp / "audio_loud.wav"
        silent.write_bytes(b"x")
        loud.write_bytes(b"x")
        orch.audio.chunk_levels[silent.name] = 0.0
        orch.audio.chunk_levels[loud.name] = 0.5
        orch.audio.last_level = 0.0   # شبیه‌سازی رِیس قدیمی: آخرین سطح متعلق به قطعه‌ی بی‌صدا است
        orch._stt_running = True
        task = asyncio.get_running_loop().create_task(orch._stt_worker())
        await orch._on_audio_chunk(silent)
        await orch._on_audio_chunk(loud)
        await asyncio.sleep(0.4)
        orch._stt_running = False
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    texts = [t[2] for t in orch.watcher._transcript]
    assert texts == ["متن audio_loud"], texts   # بی‌صدا حذف؛ پرصدا با وجود last_level=0 تبدیل شد
    assert ai.calls == 1


def test_monitor_for_region_picks_containing_monitor():
    from app.live_watcher import clip_region_to_monitor, monitor_for_region

    m1 = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    m2 = {"left": 1920, "top": 0, "width": 2560, "height": 1440}
    # ناحیه روی مانیتور دوم
    assert monitor_for_region([m1, m2], {"left": 2000, "top": 100, "width": 1200, "height": 800}) is m2
    # ناحیه‌ی نیمه‌بیرون از مانیتور اول ولی با هم‌پوشانی بیشتر با دوم → دوم و کلیپ
    part = {"left": 1800, "top": 0, "width": 400, "height": 800}
    mon = monitor_for_region([m1, m2], part)
    assert mon is m2
    c = clip_region_to_monitor(part, mon)
    assert c == {"left": 1920, "top": 0, "width": 280, "height": 800}
    # بیرون از همه‌ی مانیتورها
    assert monitor_for_region([m1, m2], {"left": 9000, "top": 0, "width": 400, "height": 400}) is None
    # تک‌مانیتور
    assert monitor_for_region([m1], {"left": 100, "top": 100, "width": 200, "height": 200}) is m1


class _GateVisionAI:
    """مدل بینایی ساختگی که تا سیگنال رهایی، پاسخ نمی‌دهد (تست هم‌پوشانی تحلیل)."""

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()
        self.precheck_enabled = False

    async def extract_live_events(self, *a, **k):  # noqa: ARG002
        self.calls += 1
        await self.release.wait()
        return {"frame": {"type": "other"}, "events": []}, ""


def test_pending_chart_event_survives_busy_vision():
    """رویداد چارت حین تحلیل قبلی نباید گم شود؛ بلافاصله پس از اتمام تحلیل اجرا می‌شود."""
    w, _, _ = make_watcher()
    ai = _GateVisionAI()
    w.ai = ai  # type: ignore[assignment]
    info = FrameInfo(valid=True, kind="local")

    async def scenario():
        t1 = w._maybe_process(_small_img(), "chart", info)
        assert t1 is not None and not t1.done()
        # محرک گفتاری حین تحلیل: دوباره مسلح می‌شود (رفتار سابق)
        assert w._maybe_process(_small_img(), "speech", info) is None
        assert w._speech_trigger.is_set()
        # رویداد چارت حین تحلیل: pending می‌شود، نه گم‌شده
        assert w._maybe_process(_small_img(), "chart", info) is None
        assert w._pending_chart
        ai.release.set()
        await asyncio.wait_for(t1, timeout=3)
        # حلقه‌ی اصلی پس از آزادشدن مدل pending را اجرا می‌کند (cooldown ریست می‌شود)
        w._last_vision = 0.0
        w._pending_chart = False
        t3 = w._maybe_process(_small_img(), "chart", info)
        assert t3 is not None
        await asyncio.wait_for(t3, timeout=3)
        assert ai.calls == 2

    asyncio.run(scenario())
    assert "vision_busy" in w.stats() and "pending_chart" in w.stats()


class _FakeHTTPResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict:
        return {"price": 4000.0}


class _FakeHTTPClient:
    def __init__(self, owner: "_CountingHTTPMarket"):
        self.owner = owner
        self.is_closed = False

    async def get(self, url: str, headers: dict | None = None) -> _FakeHTTPResponse:  # noqa: ARG002
        self.owner.requests += 1
        return _FakeHTTPResponse()

    async def aclose(self) -> None:
        self.is_closed = True


class _CountingHTTPMarket(MarketPriceProvider):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.clients = 0
        self.requests = 0

    def _new_http_client(self) -> _FakeHTTPClient:  # type: ignore[override]
        self.clients += 1
        return _FakeHTTPClient(self)


def test_http_price_reuses_client():
    """کلاینت HTTP قیمت باید پایا بماند (بدون TLS handshake در هر فراخوانی) و پس از aclose تازه بسازد."""
    cfg = copy.deepcopy(CFG)
    cfg["market"] = {"sources": ["http"], "http": {"url": "https://x.test/price/{symbol}", "json_path": "price"}}
    mp = _CountingHTTPMarket(cfg)

    async def scenario():
        p1 = await mp._http_price("XAUUSD")
        p2 = await mp._http_price("XAUUSD")
        assert p1 == 4000.0 and p2 == 4000.0
        assert mp.clients == 1 and mp.requests == 2, (mp.clients, mp.requests)
        await mp.aclose()
        await mp._http_price("XAUUSD")
        assert mp.clients == 2

    asyncio.run(scenario())


def test_publisher_no_idle_writes_and_sent_prune():
    """flush بدون تغییر نباید فایل بنویسد؛ نگاشت sent هم نباید بی‌نهایت بزرگ شود."""
    with tempfile.TemporaryDirectory() as td:
        cfg = _pub_cfg()
        cfg["publisher"]["dry_run"] = True
        pub = TelegramPublisher(cfg, Path(td))
        pub.enqueue("A")
        asyncio.run(pub.flush_once())          # ارسال dry-run + ذخیره
        mtimes1 = {p.name: p.stat().st_mtime_ns for p in Path(td).glob("*.json")}
        assert mtimes1
        asyncio.run(pub.flush_once())          # صف خالی → هیچ فایلی نباید بازنویسی شود
        mtimes2 = {p.name: p.stat().st_mtime_ns for p in Path(td).glob("*.json")}
        assert mtimes1 == mtimes2
        # نگاشت sent بزرگ و کهنه → پس از ذخیره هرس می‌شود
        old = time.time() - 2 * 86400
        pub._sent = {f"id{i}": {"message_id": i, "sent_at": old} for i in range(500)}
        pub._save()
        assert len(pub._sent) == 200, len(pub._sent)


# ---------------- 2.3.0 تکمیلی: ترتیب انتشار، انقضاها، پایان جلسه حین تحلیل ----------------
class _SequencedVisionAI:
    """هر فراخوانی مدل → یک رویداد ورود کامل (اول خرید، بعد فروش) برای تست ترتیب انتشار."""

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()
        self.precheck_enabled = False

    def _event(self) -> dict:
        if self.calls == 1:
            return {"intent": "ACTUAL_ENTRY", "symbol": "XAUUSD", "side": "BUY", "order_type": "MARKET",
                    "sl": 3970, "confidence": 0.95, "evidence_quote": "همین الان بخرید",
                    "speaker_is_analyst": True, "is_new_drawing": True}
        return {"intent": "ACTUAL_ENTRY", "symbol": "XAUUSD", "side": "SELL", "order_type": "MARKET",
                "sl": 3990, "confidence": 0.95, "evidence_quote": "همین الان بفروشید",
                "speaker_is_analyst": True, "is_new_drawing": True}

    async def extract_live_events(self, *a, **k):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            await self.release.wait()
        return {"frame": {"type": "chart", "symbol_on_chart": "XAUUSD", "current_price": 3982.4},
                "events": [self._event()]}, ""


def test_signal_publish_order_preserved():
    """ترتیب انتشار در کانال = ترتیب وقوع؛ تحلیل پس‌زمینه‌ی دوم زودتر از اولی به کانال نمی‌رود."""
    w, pub, trades = make_watcher()
    ai = _SequencedVisionAI()
    w.ai = ai  # type: ignore[assignment]
    info = FrameInfo(valid=True, kind="local")

    async def scenario():
        t1 = w._maybe_process(_small_img(), "chart", info)            # تحلیل ۱ (خرید) در جریان
        assert w._maybe_process(_small_img(), "chart", info) is None  # رویداد ۲ حین تحلیل → pending
        assert w._pending_chart
        ai.release.set()
        await asyncio.wait_for(t1, timeout=3)
        w._last_vision = 0.0
        w._pending_chart = False
        t2 = w._maybe_process(_small_img(), "chart", info)            # تحلیل ۲ (فروش) پس از آزادشدن
        await asyncio.wait_for(t2, timeout=3)

    asyncio.run(scenario())
    texts = [m["text"] for m in pub.sent if m["target"] == "channel"]
    assert len(texts) == 2, texts
    assert "T1" in texts[0] and "خرید" in texts[0], texts[0]
    assert "T2" in texts[1] and "فروش" in texts[1], texts[1]


def test_draft_ttl_expiry():
    """پیش‌نویس ناقص بیش از trades.draft_ttl_sec (۱۲۰ ثانیه) منقضی می‌شود؛ تکمیلِ بعدی با کهنه ادغام نمی‌شود."""
    from app import clock

    w, pub, trades = make_watcher()
    assert run(w, chart_frame(), ev(sl=None, tps=[])) == ["draft"]
    assert trades.drafts
    # کهن‌کردن پیش‌نویس فراتر از TTL
    sig, _stored = next(iter(trades.drafts.values()))
    trades.drafts[sig.symbol] = (trades.drafts[sig.symbol][0], clock.now() - trades.draft_ttl - 1)
    # رویداد بعدی هم ناقص است → پیش‌نویس کهنه منقضی و پیش‌نویس تازه (بدون ورود قدیمی ۳۹۸۰) ذخیره می‌شود
    assert run(w, chart_frame(), ev(entry=None, sl=3972, tps=[3995])) == ["draft"]
    assert trades.drafts["XAUUSD"][0].entry is None
    assert any("منقضی" in m["text"] for m in pub.sent if m["target"] == "admin")


def test_duplicate_window_expiry_creates_new_trade():
    """پس از ۳۰ دقیقه بی‌فعالیتی (update_window از آخرین فعالیت)، همان سیگنال تکراری نیست؛ معامله‌ی جدید است."""
    from app import clock

    w, pub, trades = make_watcher()
    assert run(w, chart_frame(), ev()) == ["published"]
    # بیرون بازه: نه ایجاد و نه آخرین فعالیت در ۳۰ دقیقه‌ی اخیر
    trades.trades["T1"].created_at = clock.now() - trades.update_window - 1
    trades.trades["T1"].updated_at = clock.now() - trades.update_window - 1
    assert run(w, chart_frame(), ev()) == ["published"]                       # دیگر duplicate نیست
    assert set(trades.trades) == {"T1", "T2"}
    assert len([m for m in pub.sent if m["target"] == "channel"]) == 2


def test_idle_watchdog_ends_session_after_silence():
    """پایان جلسه پس از analysis.auto_end_idle_min (۸ دقیقه) بدون گفتار و بدون تغییر چارت."""
    with patched(orch_mod.asyncio, "sleep", _nosleep):
        o = _make_orch()
        o.joiner = StubJoiner()  # type: ignore[assignment]
        o.in_meeting = True
        o.started_at = time.time()
        # آخرین گفتار و آخرین تغییر چارت: ۱۰ دقیقه پیش → بیش از ۸ دقیقه سکوت
        o.watcher.last_speech_at = time.time() - 600
        o.watcher.last_change_at = time.time() - 600
        asyncio.run(o._end_watchdog())
        assert o.in_meeting is False
        assert "بدون گفتار" in o.end_reason, o.end_reason


def test_no_channel_signal_after_end_during_vision():
    """تحلیل نیمه‌کاره حین پایان جلسه: قطع می‌شود و هیچ سیگنال کانالی بعد از پایان ساخته نمی‌شود."""
    orch = _make_orch()
    ai = _GateVisionAI()
    orch.ai = ai  # type: ignore[assignment]
    orch.watcher.ai = ai  # type: ignore[assignment]

    async def scenario():
        orch.in_meeting = True   # جلسه‌ی فعال (بدون اجرای کامل start_manual)
        task = orch.watcher._maybe_process(_small_img(), "chart", FrameInfo(valid=True, kind="local"))
        assert task is not None and not task.done()
        await asyncio.sleep(0.05)   # تحلیل وارد انتظار پاسخ مدل می‌شود
        await orch.end_meeting("تست پایان حین تحلیل")
        await asyncio.sleep(0.1)
        assert task.cancelled(), task          # تحلیل نیمه‌کاره قطع شد
        ai.release.set()                        # «پاسخ مدل» بعد از پایان می‌رسد
        await asyncio.sleep(0.1)
        # هیچ سیگنال کانالی پس از پایان (حتی با رسیدن پاسخ مدل) صف نشده است
        assert [q for q in orch.publisher._queue if q["target"] == "channel"] == []
        assert orch.publisher.pending_count() == 0

    asyncio.run(scenario())


class _CountingVisionAI:
    """مدل بینایی ساختگیِ سریع: فقط تعداد فراخوانی‌ها را می‌شمارد (بدون گیر)."""

    def __init__(self):
        self.calls = 0
        self.precheck_enabled = False

    async def extract_live_events(self, *a, **k):  # noqa: ARG002
        self.calls += 1
        return {"frame": {"type": "other"}, "events": []}, ""


def test_cooldown_chart_event_deferred_not_dropped():
    """رویداد چارت حین cooldown حذف نمی‌شود؛ بلافاصله پس از پایان cooldown با تازه‌ترین فریم
    اجرا می‌شود و رویدادهای یک بازه در «یک» تحلیل ادغام می‌شوند (سقف نرخ = vision_cooldown_sec)."""
    w, _, _ = make_watcher()
    ai = _CountingVisionAI()
    w.ai = ai  # type: ignore[assignment]
    info = FrameInfo(valid=True, kind="local")

    async def scenario():
        t1 = w._maybe_process(_small_img(), "chart", info)   # تحلیل اول → cooldown فعال می‌شود
        assert t1 is not None
        await asyncio.wait_for(t1, timeout=3)
        assert ai.calls == 1
        # مدل آزاد است ولی cooldown فعال: رویداد باید در انتظار بماند، نه حذف شود
        assert w._maybe_process(_small_img(), "chart", info) is None
        assert w._pending_chart
        # رویدادهای بیشتر در همان بازه: ادغام می‌شوند، فراخوانی مدل جدیدی نمی‌خورند
        for _ in range(5):
            assert w._maybe_process(_small_img(), "chart", info) is None
        assert ai.calls == 1 and w._pending_chart
        # dispatch حلقه وقتی cooldown هنوز تمام نشده: دوباره به انتظار برمی‌گردد
        w._pending_chart = False
        assert w._maybe_process(_small_img(), "chart", info) is None
        assert w._pending_chart
        assert ai.calls == 1
        # پایان cooldown → حلقه dispatch می‌کند: دقیقاً «یک» تحلیل برای همه‌ی رویدادها
        w._last_vision = 0.0
        w._pending_chart = False
        t2 = w._maybe_process(_small_img(), "chart", info)
        assert t2 is not None
        await asyncio.wait_for(t2, timeout=3)
        assert ai.calls == 2
        assert w._pending_chart is False

    asyncio.run(scenario())


def test_sent_map_prune_keeps_open_trade_mappings():
    """هرس sent_map نگاشت پیام معامله‌های زنده (SENT/REVIEW) را حذف نمی‌کند؛ بسته‌شده‌ها مثل بقیه هرس می‌شوند."""
    from app.trade_state import Trade

    with tempfile.TemporaryDirectory() as td:
        cfg = _pub_cfg()
        cfg["publisher"]["dry_run"] = True
        pub = TelegramPublisher(cfg, Path(td))
        tb = TradeBook(cfg, pub)
        pub.protect_ids = tb.protected_msg_ids

        t_open = Trade(id="T1", signal=Signal(symbol="XAUUSD", side="BUY", order_type="MARKET", sl=3970),
                       status="SENT")
        t_open.msg_id, t_open.last_msg_id = "open-main", "open-reply"
        t_review = Trade(id="T2", signal=Signal(symbol="EURUSD", side="SELL", order_type="MARKET", sl=1.09),
                         status="REVIEW")
        t_review.review_msg_id = "review-msg"
        t_closed = Trade(id="T3", signal=Signal(symbol="XAUUSD", side="BUY", order_type="MARKET", sl=3970),
                         status="CLOSED")
        t_closed.msg_id = "closed-msg"
        tb.trades = {"T1": t_open, "T2": t_review, "T3": t_closed}

        old = time.time() - 2 * 86400   # همه کهنه‌تر از ۲۴ ساعت
        pub._sent = {"open-main": {"message_id": 1, "sent_at": old},
                     "open-reply": {"message_id": 2, "sent_at": old},
                     "review-msg": {"message_id": 3, "sent_at": old},
                     "closed-msg": {"message_id": 4, "sent_at": old}}
        pub._sent.update({f"stale{i}": {"message_id": 100 + i, "sent_at": old} for i in range(500)})
        pub._save()
        # معامله‌ی باز و در-بازبینی: نگاشتشان حفظ می‌شود حتی کهنه‌تر از ۲۴ ساعت
        assert "open-main" in pub._sent and "open-reply" in pub._sent
        assert "review-msg" in pub._sent
        # معامله‌ی بسته: مثل بقیه هرس می‌شود
        assert "closed-msg" not in pub._sent
        assert len(pub._sent) < 500


def test_tradebook_persist_and_restore():
    """وضعیت معاملات پس از ری‌استارت بازیابی می‌شود: معامله‌ی زنده + نگاشت پیامش محافظت می‌شود؛
    معامله‌ی بسته/کهنه بازیابی نمی‌شود؛ شماره‌ی شناسه ادامه می‌یابد و تکرار بعد از ری‌استارت هم گرفته می‌شود."""
    from app import clock

    with tempfile.TemporaryDirectory() as td:
        cfg = copy.deepcopy(CFG)
        cfg["telegram"]["admin_chat"] = ""
        sf = Path(td) / "trades.json"
        pub1 = MemoryPublisher(cfg)
        tb1 = TradeBook(cfg, pub1, state_file=sf)

        s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                    tps=[3995, 4005], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)
        assert tb1.handle_entry(s1, [], "publish") == "published"
        assert sf.exists()   # ذخیره خودکار هنگام انتشار
        s2 = Signal(symbol="EURUSD", side="SELL", order_type="LIMIT", entry=1.09, sl=1.095,
                    tps=[1.08], confidence=0.95, evidence="سل لیمیت", speaker_is_analyst=True)
        assert tb1.handle_entry(s2, [], "publish") == "published"
        tb1.trades["T2"].status = "CLOSED"
        tb1.drafts["XAUUSD"] = (Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT",
                                       entry=3980, confidence=0.6), clock.now())
        tb1.save()
        msg_id_t1 = tb1.trades["T1"].msg_id

        # ——— ری‌استارت ———
        tb2 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb2.load()
        assert set(tb2.trades) == {"T1"}                       # بسته‌شده بازیابی نمی‌شود
        assert tb2.trades["T1"].msg_id == msg_id_t1
        assert "XAUUSD" in tb2.drafts                          # پیش‌نویس تازه بازیابی می‌شود
        assert tb2._seq == 2                                   # شماره‌ی شناسه ادامه می‌یابد
        assert tb2.protected_msg_ids() == {msg_id_t1}          # نگاشت معامله‌ی بازیابی‌شده محافظت می‌شود
        assert tb2.handle_entry(s1, [], "publish") == "duplicate"   # تکرار بعد از ری‌استارت هم گرفته می‌شود

        # ——— ری‌استارت بعد از کرش: معامله‌ی زنده‌ی قدیمی هم بازیابی می‌شود (تا وقتی بسته نشده) ———
        tb1.trades["T1"].created_at = clock.now() - 3600   # یک ساعت قبل باز شده
        tb1.save()
        tb3 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb3.load()
        assert set(tb3.trades) == {"T1"}                   # کرش → همه‌ی زنده‌ها بازیابی می‌شوند
        assert tb3.protected_msg_ids() == {msg_id_t1}

        # ——— پایان تمیز جلسه: شروع بعدی بازیابی نمی‌کند ———
        tb3.close_session()
        tb4 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb4.load()
        assert tb4.trades == {}
        assert tb4._seq == 2   # با این حال شماره‌ها تکراری نمی‌شوند


def test_vision_calls_counted_and_recorded_at_end():
    """تعداد فراخوانی مدل بینایی در stats شمرده می‌شود و آمار پایش در پایان جلسه در events.jsonl ثبت می‌شود."""
    orch = _make_orch()
    ai = _CountingVisionAI()
    orch.ai = ai  # type: ignore[assignment]
    orch.watcher.ai = ai  # type: ignore[assignment]
    info = FrameInfo(valid=True, kind="local")

    async def scenario():
        orch.in_meeting = True
        for _ in range(2):
            t = orch.watcher._maybe_process(_small_img(), "chart", info)
            await asyncio.wait_for(t, timeout=3)
            orch.watcher._last_vision = 0.0   # cooldown را برای فراخوانی بعدی ریست کن
            orch.watcher._pending_chart = False
        assert orch.watcher.stats()["vision_calls"] == 2
        # ضبط جلسه فعال → پایان جلسه آمار watcher را در meta می‌نویسد
        orch.recorder = SessionRecorder({**copy.deepcopy(orch.cfg), "recording": {"enabled": True}},
                                        Path(tempfile.mkdtemp()))
        orch.watcher.recorder = orch.recorder
        rec_dir = orch.recorder.start({"url": "test"})
        await orch.end_meeting("تست آمار پایان")
        return rec_dir

    rec_dir = asyncio.run(scenario())
    import json
    lines = (Path(rec_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    metas = [json.loads(l) for l in lines if l.strip() and json.loads(l)["type"] == "meta" and "watcher" in json.loads(l)]
    assert metas and metas[-1]["watcher"]["vision_calls"] == 2, metas


def test_management_any_age_and_dedup_by_activity():
    """دستور مدیریتی برای معامله‌ی باز هر چقدر هم قدیم باشد وصل می‌شود و Reply می‌خورد؛
    پنجره‌ی ۳۰ دقیقه فقط برای تشخیص تکرار سیگنال ورود جدید است و از آخرین فعالیت می‌سازد."""
    from app import clock

    cfg = copy.deepcopy(CFG)
    # الف) مدیریت معامله‌ی ۹۰ دقیقه‌ای بدون ری‌استارت
    pub_a = MemoryPublisher(cfg)
    tb_a = TradeBook(cfg, pub_a)
    s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                tps=[3995, 4005], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)
    assert tb_a.handle_entry(s1, [], "publish") == "published"
    msg_id = tb_a.trades["T1"].msg_id
    tb_a.trades["T1"].created_at = clock.now() - 5400   # ساعت ۱۰:۰۰ باز شده، حالا ۱۱:۳۰ است
    tb_a.trades["T1"].updated_at = clock.now() - 5400
    mgmt = {"intent": "MANAGEMENT", "management": {"action": "BREAKEVEN"},
            "confidence": 0.9, "evidence_quote": "ریسک فری کنید"}
    assert tb_a.handle_management(mgmt, "XAUUSD", 0.9) == "managed"
    replies = [m for m in pub_a.sent if m.get("reply_to") == msg_id]
    assert replies and "ریسک‌فری" in replies[0]["text"], replies   # روی پیام اصلی Reply خورده

    # ب) تکرار: معامله‌ی ۹۰ دقیقه پیش ساخته شده ولی ۱۰ دقیقه پیش فعالیت داشته → هنوز «همان» است
    pub_b = MemoryPublisher(cfg)
    tb_b = TradeBook(cfg, pub_b)
    assert tb_b.handle_entry(s1, [], "publish") == "published"
    tb_b.trades["T1"].created_at = clock.now() - 5400
    tb_b.trades["T1"].updated_at = clock.now() - 600     # آخرین فعالیت ۱۰ دقیقه پیش
    assert tb_b.handle_entry(s1, [], "publish") == "duplicate"

    # ج) ۳۰ دقیقه بی‌فعالیتی → سیگنال همان، ولی معامله‌ی جدید
    pub_c = MemoryPublisher(cfg)
    tb_c = TradeBook(cfg, pub_c)
    assert tb_c.handle_entry(s1, [], "publish") == "published"
    tb_c.trades["T1"].created_at = clock.now() - 3600
    tb_c.trades["T1"].updated_at = clock.now() - 3600
    assert tb_c.handle_entry(s1, [], "publish") == "published"
    assert set(tb_c.trades) == {"T1", "T2"}


def test_atomic_write_retries_on_permission_error():
    """قفل موقت فایل (آنتی‌ویروس/ایندکسر) → چند بار تلاش مجدد و در نهایت نوشتن موفق."""
    import pathlib

    from app import utils as utils_mod

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state.json"
        real = pathlib.Path.replace
        calls = {"n": 0}

        def flaky(self, target):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("قفل موقت آنتی‌ویروس")
            return real(self, target)

        pathlib.Path.replace = flaky  # type: ignore[method-assign]
        try:
            utils_mod.atomic_write_text(p, '{"ok": 1}')
        finally:
            pathlib.Path.replace = real  # type: ignore[method-assign]
        assert calls["n"] == 3
        assert p.read_text(encoding="utf-8") == '{"ok": 1}'


def test_tradebook_save_survives_locked_file():
    """شکست نهایی نوشتن (فایل همیشه قفل) نباید برنامه را بیندازد؛ فقط هشدار لاگ می‌شود."""
    import pathlib

    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "trades.json"
        tb = TradeBook(copy.deepcopy(CFG), MemoryPublisher(CFG), state_file=sf)
        real = pathlib.Path.replace

        def locked(self, target):  # noqa: ANN001
            raise PermissionError("قفل پایدار")

        pathlib.Path.replace = locked  # type: ignore[method-assign]
        try:
            tb.save()   # نباید استثنا بدهد
        finally:
            pathlib.Path.replace = real  # type: ignore[method-assign]
        assert not sf.exists()   # ذخیره نشد ولی کرش نکرد


def test_restore_staleness_gap():
    """فایل وضعیت کهنه‌تر از trades.restore_max_gap_min → هیچ معامله‌ای بازیابی نمی‌شود
    (توقف کنسول/ویندوز دیروز نباید آپدیت امروز را روی سیگنال دیروز بیندازد)؛ شماره‌ی شناسه‌ها حفظ می‌شود."""
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        cfg = copy.deepcopy(CFG)
        cfg["telegram"]["admin_chat"] = ""
        sf = Path(td) / "trades.json"
        s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                    tps=[3995, 4005], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)

        # الف) ذخیره‌ی ۵ دقیقه پیش → بازیابی می‌شود
        tb1 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        assert tb1.handle_entry(s1, [], "publish") == "published"
        msg_id = tb1.trades["T1"].msg_id
        d = _json.loads(sf.read_text(encoding="utf-8"))
        d["saved_at"] = time.time() - 300        # ۵ دقیقه پیش
        sf.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tb2 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb2.load()
        assert set(tb2.trades) == {"T1"} and tb2.trades["T1"].msg_id == msg_id

        # ب) ذخیره‌ی ۱۸ ساعت پیش بدون session_ended (توقف کنسول/خاموشی) → بدون بازیابی؛ شماره‌ها ادامه می‌یابند
        d = _json.loads(sf.read_text(encoding="utf-8"))
        d["saved_at"] = time.time() - 18 * 3600
        sf.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tb3 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb3.load()
        assert tb3.trades == {}
        assert tb3._seq == 1

        # ج) آپدیت مدیریتی به معامله‌ی قدیمی وصل نمی‌شود؛ سیگنال جدید با شماره‌ی ادامه‌یافته ساخته می‌شود
        mgmt = {"intent": "MANAGEMENT", "management": {"action": "BREAKEVEN"},
                "target_trade_id": "T1", "confidence": 0.9, "evidence_quote": "ریسک فری کنید"}
        assert tb3.handle_management(mgmt, "XAUUSD", 0.9) == "no_target"
        assert not [m for m in tb3.pub.sent if m.get("reply_to")]
        assert tb3.handle_entry(s1, [], "publish") == "published"
        assert "T2" in tb3.trades and tb3.trades["T2"].msg_id


def test_periodic_tick_save_survives_long_session_restart():
    """جلسه‌ی ۱۵۰ دقیقه‌ای با معامله‌ی بی‌تغییر: ذخیره‌ی دوره‌ی tick (حلقه‌ی پایش، هر ~۱۵ ثانیه)
    saved_at را تازه نگه می‌دارد؛ پس از ری‌استارت فوری، معامله با همان شناسه و msg_id بازیابی می‌شود."""
    import json as _json

    from app import clock

    with tempfile.TemporaryDirectory() as td:
        cfg = copy.deepcopy(CFG)
        cfg["telegram"]["admin_chat"] = ""
        sf = Path(td) / "trades.json"
        tb = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                    tps=[3995, 4005], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)
        assert tb.handle_entry(s1, [], "publish") == "published"
        msg_id = tb.trades["T1"].msg_id
        # جلسه ادامه دارد؛ معامله ۱۵۰ دقیقه پیش باز شده و دیگر تغییر نکرده
        tb.trades["T1"].created_at = clock.now() - 150 * 60
        tb.trades["T1"].updated_at = clock.now() - 150 * 60
        # آخرین ذخیره را هم قدیمی کن — بعد تیک‌های دوره‌ای آن را تازه می‌کنند
        d = _json.loads(sf.read_text(encoding="utf-8"))
        d["saved_at"] = time.time() - 150 * 60
        sf.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        # تیک‌های دوره‌ای حلقه‌ی پایش (بدون هیچ تغییر معاملاتی) — همان مسیر واقعی: tick → save
        for _ in range(3):
            tb.tick(None)
        d = _json.loads(sf.read_text(encoding="utf-8"))
        assert time.time() - d["saved_at"] < 60          # ذخیره‌ی دوره‌ای saved_at را تازه کرده
        assert d["session_ended"] is False
        # ری‌استارت فوری پس از جلسه‌ی طولانی → بازیابی با همان شناسه و msg_id
        tb2 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb2.load()
        assert set(tb2.trades) == {"T1"}
        assert tb2.trades["T1"].msg_id == msg_id
        assert tb2.protected_msg_ids() == {msg_id}


def test_late_periodic_save_cannot_unset_session_ended():
    """ذخیره‌ی عقب‌افتاده‌ی دوره‌ای پس از پایان جلسه نمی‌تواند session_ended را خنثی کند."""
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        cfg = copy.deepcopy(CFG)
        cfg["telegram"]["admin_chat"] = ""
        sf = Path(td) / "trades.json"
        tb = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                    tps=[3995], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)
        assert tb.handle_entry(s1, [], "publish") == "published"
        tb.close_session()          # پایان جلسه: پرچم نوشته شد
        tb.tick(None)               # تیک عقب‌افتاده بعد از پایان جلسه
        d = _json.loads(sf.read_text(encoding="utf-8"))
        assert d["session_ended"] is True        # پرچم خنثی نشده (حالت نمونه است، نه per-call)
        tb2 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb2.load()
        assert tb2.trades == {}                  # و بازیابی هم نمی‌شود


def test_load_does_not_refresh_stale_saved_at():
    """شروع برنامه با فایل کهنه نباید saved_at را تازه کند؛ تصمیم کهنگی باید با زمان خود فایل بماند."""
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        cfg = copy.deepcopy(CFG)
        cfg["telegram"]["admin_chat"] = ""
        sf = Path(td) / "trades.json"
        tb = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                    tps=[3995], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)
        assert tb.handle_entry(s1, [], "publish") == "published"
        d = _json.loads(sf.read_text(encoding="utf-8"))
        old_saved_at = time.time() - 10 * 3600
        d["saved_at"] = old_saved_at
        sf.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        before = sf.read_text(encoding="utf-8")
        tb2 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb2.load()
        assert sf.read_text(encoding="utf-8") == before           # load فایل را ننوشت
        assert _json.loads(before)["saved_at"] == old_saved_at
        tb3 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb3.load()
        assert tb3.trades == {}                                    # شروع دوم هم هنوز کهنه است


def test_tick_error_still_saves_and_does_not_swallow():
    """خطای کنترل‌شده در بدنه‌ی tick: save در finally اجرا می‌شود، خطای اصلی پنهان نمی‌شود و
    دیسک همیشه همان حافظه است (خطای میانی باعث ذخیره‌ی وضعیت ناسازگار نمی‌شود)."""
    import json as _json

    from app import clock

    with tempfile.TemporaryDirectory() as td:
        cfg = copy.deepcopy(CFG)
        cfg["telegram"]["admin_chat"] = ""
        sf = Path(td) / "trades.json"
        tb = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        s1 = Signal(symbol="XAUUSD", side="BUY", order_type="LIMIT", entry=3980, sl=3972,
                    tps=[3995], confidence=0.95, evidence="بای لیمیت", speaker_is_analyst=True)
        assert tb.handle_entry(s1, [], "publish") == "published"

        def boom(*a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("خطای کنترل‌شده")

        # الف) خطا «قبل از» هر جهش: ذخیره در finally اجرا می‌شود، خطا بالا می‌آید، وضعیت دست‌نخورده می‌ماند
        orig_expire = tb._expire_drafts
        tb._expire_drafts = boom  # type: ignore[method-assign]
        try:
            raised = False
            try:
                tb.tick(None)
            except RuntimeError as e:
                raised = str(e) == "خطای کنترل‌شده"
            assert raised, "خطای اصلی باید پنهان‌نشده بالا بیاید"
            d = _json.loads(sf.read_text(encoding="utf-8"))
            assert time.time() - d["saved_at"] < 60            # save در finally اجرا شد
            assert d["trades"][0]["status"] == "SENT"          # قبل از خطا جهشی نبود
        finally:
            tb._expire_drafts = orig_expire  # type: ignore[method-assign]

        # ب) خطای «میانی» بعد از یک جهش مجاز (فعال‌شدن سفارش): دیسک باید دقیقاً همان حافظه باشد
        tb.trades["T1"].status = "SENT"
        tb.trades["T1"].log = boom  # type: ignore[method-assign]
        raised = False
        try:
            tb.tick({"XAUUSD": 3975.0})   # LIMIT BUY و 3975<=3980 → ACTIVE، سپس t.log → خطا
        except RuntimeError:
            raised = True
        assert raised
        d = _json.loads(sf.read_text(encoding="utf-8"))
        assert d["trades"][0]["status"] == "ACTIVE"            # جهش مجاز ذخیره شد
        assert time.time() - d["saved_at"] < 60
        tb3 = TradeBook(cfg, MemoryPublisher(cfg), state_file=sf)
        tb3.load()
        assert tb3.trades["T1"].status == tb.trades["T1"].status == "ACTIVE"   # دیسک == حافظه


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"✅ {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"❌ {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
