"""تست سناریوهای لایوترید بدون API و تلگرام.

اجرا:  python -m pytest -q tests   یا   python tests/test_scenarios.py
هر تست یک حالت از نقشه‌ی حالت‌ها را پوشش می‌دهد.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import load_config  # noqa: E402
from app.frames import FrameAnalyzer  # noqa: E402
from app.latency import LatencyTracker, compute_stages  # noqa: E402
from app.lexicon import merge_overlap, normalize_trading_text, speech_flags, words_to_numbers  # noqa: E402
from app.live_watcher import ChartWatcher  # noqa: E402
from app.market import MarketPriceProvider  # noqa: E402
from app.publisher import MemoryPublisher  # noqa: E402
from app.region_picker import (  # noqa: E402
    RegionError, logical_to_physical, physical_to_logical, selection_to_region, validate_region,
    write_region_to_config,
)
from app.signal_model import BuildContext, build_signal, route, validate  # noqa: E402
from app.symbols import expand_short_price, get_spec, normalize_symbol, to_float  # noqa: E402
from app.trade_state import TradeBook  # noqa: E402

CFG = copy.deepcopy(load_config())
CFG["telegram"]["admin_chat"] = "admin"
CFG["recording"] = {"enabled": False}


class FixedMarket(MarketPriceProvider):
    def __init__(self, prices: dict[str, float], independent: bool = True, open_: bool = True):
        super().__init__(CFG)
        self.prices, self.independent, self.open_ = prices, independent, open_

    async def get_price(self, symbol):  # noqa: ANN001
        p = self.prices.get(symbol)
        return (p, "test", self.independent) if p else (None, "none", False)

    def market_open(self, symbol, now=None):  # noqa: ANN001
        return self.open_


def make_watcher(prices=None, independent=True, open_=True):
    pub = MemoryPublisher(CFG)
    trades = TradeBook(CFG, pub)
    w = ChartWatcher(CFG, None, pub, ROOT / "session_data/test_tmp",  # type: ignore[arg-type]
                     market=FixedMarket(prices or {"XAUUSD": 3982.4, "EURUSD": 1.0832}, independent, open_),
                     trades=trades)
    return w, pub, trades


def chart_frame(symbol="XAUUSD, 15", price=3982.4, **kw):
    return {"type": "chart", "symbol_on_chart": symbol, "timeframe": "M15", "current_price": price, **kw}


def ev(**kw):
    base = {"intent": "ACTUAL_ENTRY", "symbol": "XAUUSD", "side": "BUY", "order_type": "LIMIT",
            "entry": 3980, "sl": 3972, "tps": [3995, 4005], "confidence": 0.92,
            "evidence_quote": "بای لیمیت ۳۹۸۰ استاپ ۳۹۷۲ تی پی ۳۹۹۵", "speaker_is_analyst": True,
            "is_new_drawing": True}
    base.update(kw)
    return base


def run(w, frame, *events):
    return asyncio.run(w.handle_model_output({"frame": frame, "events": list(events)}))


def channel(pub):
    return [m for m in pub.sent if m["target"] == "channel"]


def admin(pub):
    return [m for m in pub.sent if m["target"] == "admin"]


# ---------------- بخش ۵: نماد و اعداد ----------------
def test_symbols():
    assert normalize_symbol("XAUUSDm") == "XAUUSD"
    assert normalize_symbol("GOLD") == "XAUUSD"
    assert normalize_symbol("XAUUSD.m") == "XAUUSD"
    assert normalize_symbol("انس طلا") == "XAUUSD"
    assert normalize_symbol("یورو دلار") == "EURUSD"
    assert normalize_symbol("EURUSD, 15") is None or normalize_symbol("EURUSD, 15") == "EURUSD"
    assert normalize_symbol("EURUSD") == "EURUSD"
    assert normalize_symbol("USTEC") == "NAS100"
    assert normalize_symbol("داوجونز") == "US30"


def test_numbers():
    gold, eur, jpy = get_spec("XAUUSD"), get_spec("EURUSD"), get_spec("USDJPY")
    assert expand_short_price(85, 3982.4, gold) == 3985
    assert expand_short_price(85.5, 3982.4, gold) == 3985.5
    assert expand_short_price(10, 3995, gold) == 4010
    assert expand_short_price(3985.5, 3982.4, gold) == 3985.5
    assert expand_short_price(50, 1.0832, eur) == 1.085
    assert expand_short_price(20, 154.35, jpy) == 154.2
    assert to_float("۳۹۸۵٫۵") == 3985.5
    assert to_float("3,985.50") == 3985.5
    assert to_float("1,0850") == 1.085
    assert to_float("٣٩٨٥") == 3985
    assert words_to_numbers("استاپ سه هزار و نهصد و هشتاد و پنج و نیم") == "استاپ 3985.5"
    assert words_to_numbers("ورود هشتاد و پنج") == "ورود 85"
    assert words_to_numbers("ورود ۸۵ و نیم") in ("ورود ۸۵ و نیم", "ورود 85.5")
    assert "85.5" in normalize_trading_text("ورود ۸۵ و نیم")
    assert words_to_numbers("نه ببخشید") == "نه ببخشید"


def test_stt_fixes_and_flags():
    assert "سل لیمیت" in normalize_trading_text("سر لیمیت بذارید")
    assert "تی پی" in normalize_trading_text("تی‌پی یک")
    assert "NEGATION" in speech_flags("اینجا نخرید")
    assert "HEDGE" in speech_flags("اگه بیاد اینجا شاید بخریم")
    assert "PAST" in speech_flags("دیروز اینجا خریدیم و تی پی خورد")
    assert not speech_flags("همین الان بخرید")
    assert merge_overlap("ورود سه هزار و نهصد", "و نهصد و هشتاد") == "و هشتاد"


# ---------------- بخش ۱: انواع اعلام سیگنال ----------------
def test_clean_limit_published():
    w, pub, trades = make_watcher()
    assert run(w, chart_frame(), ev()) == ["published"]
    msg = channel(pub)[0]["text"]
    assert "Buy Limit" in msg and "3980.00" in msg and "3972.00" in msg and "TP2: 4005.00" in msg


def test_market_entry_uses_market_price():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(order_type="MARKET", entry=None, evidence_quote="همین الان بخرید استاپ ۷۲"))
    assert "3982.40" in channel(pub)[0]["text"] and "قیمت لحظه" in channel(pub)[0]["text"]


def test_short_numbers_expanded():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(entry=80, sl=72, tps=[95, 5]))
    t = channel(pub)[0]["text"]
    assert "3980.00" in t and "3972.00" in t and "4005.00" in t


def test_zone_entry():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(entry=None, entry_low=3980, entry_high=3976, sl=3970))
    assert "ناحیه‌ی ورود: 3976.00 تا 3980.00" in channel(pub)[0]["text"]


def test_scaled_entries():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(entry=None, entries=[{"price": 3980, "portion": 0.5}, {"price": 3975, "portion": 0.5}],
                             sl=3968))
    assert "ورود پله‌ای" in channel(pub)[0]["text"]


def test_sl_in_pips_gold_needs_review():
    w, pub, trades = make_watcher()
    r = run(w, chart_frame(), ev(sl=None, sl_pips=50))
    assert r == ["review"]          # تعریف پیپ طلا مبهم است
    assert not channel(pub) and "approve:" in str(admin(pub)[-1]["buttons"])


def test_sl_in_pips_eurusd_ok():
    w, pub, _ = make_watcher()
    run(w, chart_frame("EURUSD", 1.0832), ev(symbol="EURUSD", entry=1.0820, sl=None, sl_pips=30, tps=[1.0880],
                                            evidence_quote="بای لیمیت ۱.۰۸۲۰ استاپ ۳۰ پیپ"))
    assert "1.07900" in channel(pub)[0]["text"]


def test_open_tp_and_r_multiple():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(tps=[], tp_open=True))
    assert "تارگت: باز" in channel(pub)[0]["text"]
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(tps=[], tp_r_multiples=[3]))
    assert "4004.00" in channel(pub)[0]["text"]   # 3980 + 3×8


def test_tp_order_fixed():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(tps=[4005, 3995]))
    assert "TP1: 3995.00 | TP2: 4005.00" in channel(pub)[0]["text"]


def test_conditional_labeled():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(intent="CONDITIONAL", order_type="STOP", entry=4001, sl=3990, tps=[4020],
                             condition="بسته شدن کندل H4 بالای 4000"))
    t = channel(pub)[0]["text"]
    assert "سیگنال شرطی" in t and "شرط:" in t


# ---------------- بخش ۳: سیگنال واقعی/غیرواقعی ----------------
def test_non_signals_never_published():
    for intent in ("HYPOTHETICAL", "REVIEW_PAST", "BIAS_ONLY", "NOT_A_SIGNAL"):
        w, pub, _ = make_watcher()
        run(w, chart_frame(), ev(intent=intent))
        assert not channel(pub), intent


def test_safety_net_negation():
    w, pub, _ = make_watcher()
    r = run(w, chart_frame(), ev(evidence_quote="اینجا نخرید، استاپ ۷۲"))
    assert r == ["review"] and not channel(pub)


def test_safety_net_past():
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(evidence_quote="دیروز اینجا خریدیم")) == ["review"]


def test_image_only_needs_review_and_old_drawing_rejected():
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(evidence_quote="")) == ["review"]
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(evidence_quote="", is_new_drawing=False)) == ["rejected"]


def test_cancel():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev())
    tid = next(iter(trades.trades))
    run(w, chart_frame(), {"intent": "CANCEL", "symbol": "XAUUSD", "target_trade_id": tid, "confidence": 0.9,
                           "evidence_quote": "لیمیت رو پاک کنید"})
    assert trades.trades[tid].status == "CANCELLED"
    assert channel(pub)[-1]["reply_to"] == trades.trades[tid].msg_id


# ---------------- بخش ۸: اعتبارسنجی ----------------
def test_validation_rules():
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(sl=3990)) == ["rejected"]                       # SL wrong side
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(tps=[3970])) == ["rejected"]                    # TP wrong side
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(entry=4200, sl=4190, tps=[4250])) == ["rejected"]  # far from market
    w, pub, _ = make_watcher(independent=False)
    assert run(w, chart_frame(), ev(entry=4200, sl=4190, tps=[4250])) == ["review"]    # منبع غیرمستقل
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(entry=3990, sl=3985, tps=[4000])) == ["review"]  # buy limit بالای قیمت
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(tps=[3980.5])) == ["review"]                     # RR غیرعادی
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(confidence=0.3)) == ["rejected"]
    w, pub, _ = make_watcher()
    assert run(w, chart_frame(), ev(confidence=0.7)) == ["review"]


def test_inferred_type_fixed_silently():
    w, pub, _ = make_watcher()
    run(w, chart_frame(), ev(order_type=None, entry=3990, sl=3983, tps=[4005]))
    assert "Buy Stop" in channel(pub)[0]["text"]


def test_symbol_mismatch_review():
    w, pub, _ = make_watcher()
    assert run(w, chart_frame("EURUSD", 1.0832), ev()) == ["review"]


def test_market_closed_review():
    w, pub, _ = make_watcher(open_=False)
    assert run(w, chart_frame(), ev()) == ["review"]


def test_stale_stream_detection():
    w, pub, _ = make_watcher(prices={"XAUUSD": 3982.4})
    r = run(w, chart_frame(price=3700), ev(entry=3698, sl=3690, tps=[3710]))
    assert w.stale_stream and r != ["published"]


# ---------------- بخش ۹: ماشین حالت ----------------
def test_draft_merge():
    w, pub, trades = make_watcher()
    assert run(w, chart_frame(), ev(sl=None, tps=[])) == ["draft"]
    assert run(w, chart_frame(), ev(entry=None, sl=3972, tps=[3995])) == ["published"]
    assert "3980.00" in channel(pub)[0]["text"]


def test_duplicate_and_update():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev())
    assert run(w, chart_frame(), ev()) == ["duplicate"]
    assert run(w, chart_frame(), ev(entry=3979, sl=3970)) == ["updated"]
    edits = [m for m in channel(pub) if m["edit_of"]]
    assert edits and "اصلاح شد" in edits[0]["text"]
    assert len(trades.trades) == 1


def test_management_reply():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev())
    tid = next(iter(trades.trades))
    run(w, chart_frame(), {"intent": "MANAGEMENT", "symbol": "XAUUSD", "confidence": 0.9,
                           "management": {"action": "BREAKEVEN"}, "evidence_quote": "ریسک فری کنید"})
    assert trades.trades[tid].status == "BREAKEVEN" and trades.trades[tid].signal.sl == 3980
    run(w, chart_frame(), {"intent": "MANAGEMENT", "symbol": "XAUUSD", "confidence": 0.9,
                           "management": {"action": "PARTIAL_CLOSE", "percent": 50}})
    run(w, chart_frame(), {"intent": "MANAGEMENT", "symbol": "XAUUSD", "confidence": 0.9,
                           "management": {"action": "TP_HIT", "tp_index": 2}})
    replies = [m for m in channel(pub) if m["reply_to"]]
    assert len(replies) == 3 and "50٪" in replies[1]["text"]
    assert trades.trades[tid].status == "CLOSED"


def test_reverse_trade():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev(order_type="MARKET", entry=None))
    r = run(w, chart_frame(),
            {"intent": "MANAGEMENT", "symbol": "XAUUSD", "confidence": 0.9, "management": {"action": "CLOSE"}},
            ev(side="SELL", order_type="MARKET", entry=None, sl=3992, tps=[3965],
               evidence_quote="ببندید و همین الان سل بگیرید استاپ ۹۲"))
    assert r == ["managed", "published"]
    assert [t.status for t in trades.trades.values()] == ["CLOSED", "SENT"]


def test_admin_approval():
    w, pub, trades = make_watcher()
    run(w, chart_frame(), ev(confidence=0.7))
    tid = next(iter(trades.trades))
    assert asyncio.run(trades.on_callback("approve", tid)) == "ارسال شد ✅"
    assert channel(pub) and trades.trades[tid].status == "SENT"


# ---------------- بخش ۴: فریم‌ها ----------------
def test_frames():
    fa = FrameAnalyzer(CFG)
    black = Image.new("RGB", (800, 450), (0, 0, 0))
    assert not fa.analyze(black).valid
    rng = np.random.default_rng(1)
    base = (rng.random((450, 800, 3)) * 255).astype("uint8")
    fa.analyze(Image.fromarray(base))
    local = base.copy()
    local[100:160, 200:400] = 255
    info = fa.analyze(Image.fromarray(local))
    assert info.kind == "local" and fa.is_event(info) and info.bbox[0] < 0.3
    edge = local.copy()
    edge[:, 760:] = 0
    assert fa.analyze(Image.fromarray(edge)).kind == "live_edge"
    glob = (255 - edge).astype("uint8")   # تعویض کامل صفحه
    assert fa.analyze(Image.fromarray(glob)).kind == "global"


def test_market_hours():
    sat = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    mon = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    assert not MarketPriceProvider.market_open("XAUUSD", sat)
    assert MarketPriceProvider.market_open("XAUUSD", mon)
    assert MarketPriceProvider.market_open("BTCUSD", sat)


def test_route_unit():
    sig, issues = build_signal(ev(), BuildContext(ref_price=3982.4))
    issues += validate(sig, get_spec("XAUUSD"), 3982.4, True, CFG)
    assert route(sig, issues, CFG) == "publish"


# ---------------- منطقه‌ی چارت و DPI ----------------
def test_region_dpi_conversion():
    # انتخاب روی بوم 800×450 از تصویر 1600×900 مانیتور 2 → پیکسل فیزیکی همان ×2
    sel = (100, 50, 500, 350)
    mon = {"left": 0, "top": 0, "width": 1600, "height": 900}
    r = selection_to_region(sel, (800, 450), (1600, 900), mon)
    assert r == {"left": 200, "top": 100, "width": 800, "height": 600}
    # مانیتور دوم با مبدأ منفی/مثبت
    mon2 = {"left": -1920, "top": 0, "width": 1920, "height": 1080}
    r2 = selection_to_region((0, 0, 960, 540), (960, 540), (1920, 1080), mon2)
    assert r2["left"] == -1920 and r2["top"] == 0 and r2["width"] == 1920
    # مقیاس 125٪: مختصات منطقی وین‌دوز ↔ فیزیکی
    assert logical_to_physical((100, 80, 400, 300), 1.25) == (125, 100, 500, 375)
    assert physical_to_logical((125, 100, 500, 375), 1.25) == (100, 80, 400, 300)
    assert logical_to_physical((100, 100, 600, 400), 1.5) == (150, 150, 900, 600)
    # رفت‌وبرگشت در همه‌ی مقیاس‌های رایج
    for scale in (1.0, 1.25, 1.5, 2.0):
        p = logical_to_physical((137, 91, 1233, 677), scale)
        assert physical_to_logical(p, scale) == (137, 91, 1233, 677)


def test_region_validation():
    mons = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
    ok = validate_region({"left": 100, "top": 50, "width": 800, "height": 600}, mons)
    assert ok == mons[0]
    assert validate_region({"x": 10, "y": 20, "width": 900, "height": 700}, mons)  # شکل x/y هم قبول است
    for bad in ({"left": 1900, "top": 0, "width": 800, "height": 600},
                {"left": -10, "top": 0, "width": 800, "height": 600},
                {"left": 0, "top": 0, "width": 100, "height": 50},
                None):
        try:
            validate_region(bad, mons)
            raise AssertionError(f"باید خطا می‌داد: {bad}")
        except RegionError:
            pass


def test_region_config_write(tmp_path=None):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("live:\n  scan_interval_sec: 3\n  region: null   # توضیح قدیمی\n  context_sec: 75\n")
        path = Path(f.name)
    write_region_to_config(path, {"left": 12, "top": 34, "width": 1000, "height": 500},
                           {"screen_w": 1600, "screen_h": 900, "scale": 1.5})
    text = path.read_text(encoding="utf-8")
    import yaml

    cfg = yaml.safe_load(text)
    assert cfg["live"]["region"] == {"left": 12, "top": 34, "width": 1000, "height": 500}
    assert cfg["live"]["region_meta"]["scale"] == 1.5
    assert cfg["live"]["scan_interval_sec"] == 3 and cfg["live"]["context_sec"] == 75
    path.unlink()


# ---------------- ثبت تأخیر مراحل ----------------
def test_latency_stages_math():
    # قطعه‌ی 10 ثانیه‌ای: t0=100، t1=110، stt تا 113، تحلیل 113→120، تصمیم تا 120.5
    s = compute_stages({"t0": 100.0, "t1": 110.0, "stt_start": 110.0, "stt_end": 113.0},
                       vision_start=113.0, vision_end=120.0, decision_end=120.5)
    assert s["audio_wait"] == 10.0
    assert s["stt"] == 3.0
    assert s["trigger_wait"] == 0.0
    assert s["vision"] == 7.0
    assert s["decision"] == 0.5
    assert s["telegram"] is None          # تا ارسال، ثبت نشده
    # بدون صدا (سیگنال فقط تصویری): audio_wait و stt خالی می‌مانند
    s2 = compute_stages(None, 10.0, 15.0, 15.2)
    assert s2["audio_wait"] is None and s2["stt"] is None and s2["vision"] == 5.0


def test_latency_tracker_end_to_end():
    import asyncio

    writes: list[dict] = []
    tr = LatencyTracker(lambda kind, **rec: writes.append({"kind": kind, **rec}))
    pub = MemoryPublisher(CFG)
    rec = tr.add("T1", "published",
                 compute_stages({"t0": 0, "t1": 10, "stt_start": 10, "stt_end": 12}, 12.0, 30.0, 30.5),
                 msg_id="m1", enqueued_at=30.5)
    assert rec["complete"] is False and "telegram" not in [k for k, v in rec["stages"].items() if v]
    # هنوز «ارسال» نشده: در ثبت جلسه‌ی کامل نیست
    assert all(not w.get("stages", {}).get("telegram") for w in writes)
    pub.on_sent = tr.on_sent
    pub.enqueue("تست")  # پیام جدید؛ فقط m1 مربوط به سیگنال است
    # شبیه‌سازی رویداد ارسال همان پیام
    tr.on_sent("m1", 33.5)
    assert rec["complete"] and rec["stages"]["telegram"] == 3.0
    assert rec["stages"]["total"] == 33.5 - 0  # 10+2+18+0.5+3
    s = tr.summary()
    assert s["vision"]["max"] == 18.0 and s["total"]["avg"] == 33.5
    assert tr.biggest_stage() == "vision"


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
