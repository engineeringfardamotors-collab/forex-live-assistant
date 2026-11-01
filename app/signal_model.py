"""مدل سیگنال، ساخت سیگنال از خروجی مدل، اعتبارسنجی منطقی و تصمیم ارسال.

اصل طلایی: سیگنال اشتباه خطرناک‌تر از سیگنال جاافتاده است. هر ابهامی به
«بازبینی» یا «رد» می‌رود، نه به کانال.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.symbols import SymbolSpec, expand_short_price, get_spec, normalize_symbol, to_float

INTENTS = [
    "ACTUAL_ENTRY",    # ورود قطعی، همین الان
    "PENDING_ORDER",   # سفارش معلق قطعی
    "CONDITIONAL",     # «اگه ... شد»
    "HYPOTHETICAL",    # مثال یا فرضیه
    "REVIEW_PAST",     # مرور معامله گذشته
    "MANAGEMENT",      # ریسک‌فری، بستن و ...
    "CANCEL",          # لغو
    "BIAS_ONLY",       # فقط دید کلی
    "NOT_A_SIGNAL",
]
ENTRY_INTENTS = {"ACTUAL_ENTRY", "PENDING_ORDER", "CONDITIONAL"}
SILENT_INTENTS = {"HYPOTHETICAL", "REVIEW_PAST", "BIAS_ONLY", "NOT_A_SIGNAL"}

FATAL, REVIEW, INFO = "fatal", "review", "info"


@dataclass
class Issue:
    code: str
    severity: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class Signal:
    symbol: str
    side: str | None                 # BUY | SELL
    order_type: str | None           # MARKET | LIMIT | STOP
    intent: str = "ACTUAL_ENTRY"
    entry: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    entries: list[dict[str, float]] = field(default_factory=list)
    sl: float | None = None
    tps: list[float] = field(default_factory=list)
    tp_open: bool = False
    condition: str | None = None
    timeframe: str | None = None
    confidence: float = 0.0
    sources: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""
    evidence_visual: str = ""
    notes: list[str] = field(default_factory=list)
    entry_is_market: bool = False
    order_type_inferred: bool = False
    pip_converted: bool = False
    chart_symbol: str | None = None
    speaker_is_analyst: bool | None = None
    conflict: bool = False
    is_new_drawing: bool | None = None
    drawing_tool: str | None = None
    created_at: float = field(default_factory=time.time)
    spoken_at: float | None = None

    def entry_ref(self) -> float | None:
        if self.entry is not None:
            return self.entry
        if self.entry_low is not None and self.entry_high is not None:
            return (self.entry_low + self.entry_high) / 2
        if self.entries:
            return self.entries[0].get("price")
        return None

    def is_complete(self) -> bool:
        has_entry = self.entry_ref() is not None or self.order_type == "MARKET"
        return bool(self.symbol and self.side and has_entry and self.sl is not None)

    def core_key(self, spec: SymbolSpec) -> tuple:
        r = lambda v: None if v is None else round(v, spec.digits)  # noqa: E731
        return (self.symbol, self.side, self.order_type, r(self.entry_ref()), r(self.sl),
                tuple(r(t) for t in self.tps))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _conf(value: Any) -> float:
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 100 if v > 1 else max(0.0, v)
    return {"high": 0.85, "medium": 0.6, "low": 0.3}.get(str(value).lower(), 0.0)


def _side(value: Any) -> str | None:
    v = str(value or "").strip().upper()
    return {"BUY": "BUY", "LONG": "BUY", "SELL": "SELL", "SHORT": "SELL"}.get(v)


def _otype(value: Any) -> str | None:
    v = str(value or "").strip().upper().replace("BUY_", "").replace("SELL_", "")
    return v if v in {"MARKET", "LIMIT", "STOP"} else None


@dataclass
class BuildContext:
    ref_price: float | None = None       # قیمت مرجع (بازار یا نمودار) برای تکمیل اعداد خلاصه
    frame_symbol: str | None = None      # نماد خوانده‌شده از عنوان نمودار
    last_symbol: str | None = None
    timeframe: str | None = None
    symbol_aliases: dict[str, str] | None = None
    spec_overrides: dict[str, dict] | None = None
    max_short_dev_pct: float = 2.0


def build_signal(event: dict[str, Any], ctx: BuildContext) -> tuple[Signal | None, list[Issue]]:
    """تبدیل رویداد خام مدل به Signal نرمال‌شده. برمی‌گرداند: (سیگنال، مسائل یافت‌شده)."""
    issues: list[Issue] = []
    ev_symbol = normalize_symbol(event.get("symbol"), ctx.symbol_aliases)
    frame_symbol = normalize_symbol(ctx.frame_symbol, ctx.symbol_aliases)
    symbol = ev_symbol or frame_symbol or ctx.last_symbol
    if not symbol:
        return None, [Issue("SYMBOL_UNKNOWN", FATAL, "نماد مشخص نیست")]
    if ev_symbol and frame_symbol and ev_symbol != frame_symbol:
        issues.append(Issue("SYMBOL_MISMATCH", REVIEW,
                            f"نماد گفتار ({ev_symbol}) با عنوان نمودار ({frame_symbol}) فرق دارد"))
    spec = get_spec(symbol, ctx.spec_overrides)
    ref = ctx.ref_price

    def price(raw: Any, label: str, above: float | None = None, below: float | None = None) -> float | None:
        v = to_float(raw)
        if v is None:
            return None
        full = expand_short_price(v, ref, spec, ctx.max_short_dev_pct, above=above, below=below)
        if full is None:
            issues.append(Issue("UNRESOLVED_NUMBER", REVIEW,
                                f"عدد {label}={v:g} با قیمت مرجع قابل تکمیل نبود"))
        elif ref is None and v < spec.short_unit * 1000 and spec.kind in {"metal", "index", "crypto"}:
            issues.append(Issue("SHORT_NUMBER_NO_REF", REVIEW,
                                f"عدد {label}={v:g} احتمالاً خلاصه است ولی قیمت مرجع در دسترس نیست"))
        return full

    sig = Signal(
        symbol=symbol,
        side=_side(event.get("side")),
        order_type=_otype(event.get("order_type")),
        intent=str(event.get("intent") or "NOT_A_SIGNAL").upper(),
        timeframe=ctx.timeframe,
        confidence=_conf(event.get("confidence")),
        sources=event.get("field_sources") or {},
        evidence=str(event.get("evidence_quote") or ""),
        evidence_visual=str(event.get("evidence_visual") or ""),
        tp_open=bool(event.get("tp_open")),
        condition=event.get("condition") or None,
        chart_symbol=frame_symbol,
        speaker_is_analyst=event.get("speaker_is_analyst"),
        conflict=bool(event.get("conflict")),
        is_new_drawing=event.get("is_new_drawing"),
        drawing_tool=event.get("drawing_tool"),
    )
    if event.get("notes"):
        sig.notes.append(str(event["notes"]))

    sig.entry = price(event.get("entry"), "entry")
    sig.entry_low = price(event.get("entry_low"), "entry_low")
    sig.entry_high = price(event.get("entry_high"), "entry_high")
    for e in event.get("entries") or []:
        p = price((e or {}).get("price"), "entries")
        if p is not None:
            portion = to_float((e or {}).get("portion"))
            sig.entries.append({"price": p, "portion": portion if portion else 0.0})
    if sig.entries and sig.entry is None:
        sig.entry = sig.entries[0]["price"]
    if sig.entry_low is not None and sig.entry_high is not None and sig.entry_low > sig.entry_high:
        sig.entry_low, sig.entry_high = sig.entry_high, sig.entry_low
        issues.append(Issue("ZONE_SWAPPED", INFO, "کران‌های ناحیه‌ی ورود جابه‌جا اصلاح شد"))

    # ورود از قیمت فعلی
    if sig.order_type == "MARKET" and sig.entry_ref() is None and ref is not None:
        sig.entry = round(ref, spec.digits)
        sig.entry_is_market = True

    # استنتاج نوع سفارش از جای ورود نسبت به قیمت
    entry_ref = sig.entry_ref()
    if sig.order_type is None and sig.side and entry_ref is not None and ref:
        if abs(entry_ref - ref) / ref < 0.0005:
            sig.order_type = "MARKET"
        elif (sig.side == "BUY") == (entry_ref < ref):
            sig.order_type = "LIMIT"
        else:
            sig.order_type = "STOP"
        sig.order_type_inferred = True
    elif sig.order_type is None and sig.intent == "ACTUAL_ENTRY":
        sig.order_type = "MARKET"
        sig.order_type_inferred = True

    # حد ضرر
    e0 = sig.entry_ref()
    sig.sl = price(event.get("sl"), "sl",
                   below=e0 if sig.side == "BUY" else None, above=e0 if sig.side == "SELL" else None)
    sl_pips = to_float(event.get("sl_pips"))
    base = sig.entry_ref() if sig.entry_ref() is not None else ref
    if sig.sl is None and sl_pips and base is not None and sig.side:
        dist = sl_pips * spec.pip_size
        sig.sl = round(base - dist if sig.side == "BUY" else base + dist, spec.digits)
        sig.pip_converted = True
        sig.notes.append(f"استاپ {sl_pips:g} پیپ ⇒ {sig.sl} (هر پیپ {spec.pip_size:g})")
        if spec.pip_ambiguous:
            issues.append(Issue("PIP_AMBIGUOUS", REVIEW,
                                f"تعریف پیپ در {symbol} بین تحلیلگرها متفاوت است؛ تبدیل {sl_pips:g} پیپ نیاز به تأیید دارد"))

    # اهداف
    prev_tp = e0
    for t in event.get("tps") or []:
        # تارگت‌های پشت‌سرهم گفتاری صعودی (خرید) یا نزولی (فروش) فرض می‌شوند: «۹۵، ۵» ⇒ 3995، 4005
        p = price(t, "tp", above=prev_tp if sig.side == "BUY" else None,
                  below=prev_tp if sig.side == "SELL" else None)
        if p is not None:
            sig.tps.append(p)
            prev_tp = p
    if base is not None and sig.side:
        sign = 1 if sig.side == "BUY" else -1
        for pips in event.get("tp_pips") or []:
            pv = to_float(pips)
            if pv:
                sig.tps.append(round(base + sign * pv * spec.pip_size, spec.digits))
                sig.pip_converted = True
                if spec.pip_ambiguous:
                    issues.append(Issue("PIP_AMBIGUOUS", REVIEW, f"تبدیل تارگت {pv:g} پیپی در {symbol}"))
        if sig.sl is not None:
            risk = abs(base - sig.sl)
            for r in event.get("tp_r_multiples") or []:
                rv = to_float(r)
                if rv and risk > 0:
                    sig.tps.append(round(base + sign * rv * risk, spec.digits))
                    sig.notes.append(f"تارگت 1:{rv:g} از ورود و حد ضرر محاسبه شد")
    sig.tps = list(dict.fromkeys(sig.tps))
    return sig, issues


def validate(sig: Signal, spec: SymbolSpec, market_price: float | None,
             market_independent: bool, cfg: dict[str, Any]) -> list[Issue]:
    """اعتبارسنجی منطقی. market_independent=True یعنی قیمت از منبع مستقل (MT5/API) است."""
    v = cfg.get("validation", {})
    issues: list[Issue] = []
    entry = sig.entry_ref()

    if sig.intent in ENTRY_INTENTS:
        if not sig.side:
            issues.append(Issue("NO_SIDE", FATAL, "جهت معامله مشخص نیست"))
        if entry is None and sig.order_type != "MARKET":
            issues.append(Issue("INCOMPLETE", FATAL, "نقطه‌ی ورود مشخص نیست"))
        if sig.sl is None and v.get("require_sl", True):
            issues.append(Issue("INCOMPLETE", FATAL, "حد ضرر مشخص نیست"))
    if issues:
        return issues

    ref_for_checks = entry if entry is not None else market_price
    if ref_for_checks is None or not sig.side:
        return issues

    # ۱) حد ضرر در سمت درست
    if sig.sl is not None:
        if (sig.side == "BUY" and sig.sl >= ref_for_checks) or (sig.side == "SELL" and sig.sl <= ref_for_checks):
            issues.append(Issue("SL_WRONG_SIDE", FATAL,
                                f"حد ضرر {sig.sl} برای {sig.side} در سمت اشتباه ورود {ref_for_checks} است"))
        if sig.entry_low is not None and sig.entry_high is not None:
            if (sig.side == "BUY" and sig.sl >= sig.entry_low) or (sig.side == "SELL" and sig.sl <= sig.entry_high):
                issues.append(Issue("SL_INSIDE_ZONE", FATAL, "حد ضرر داخل ناحیه‌ی ورود است"))
        dist_pct = abs(ref_for_checks - sig.sl) / ref_for_checks * 100
        if dist_pct > spec.max_sl_pct:
            issues.append(Issue("SL_TOO_FAR", REVIEW, f"فاصله‌ی حد ضرر {dist_pct:.2f}٪ غیرعادی است"))
        if abs(ref_for_checks - sig.sl) < spec.pip_size * float(v.get("min_sl_pips", 2)):
            issues.append(Issue("SL_TOO_CLOSE", REVIEW, "حد ضرر بیش از حد نزدیک ورود است"))

    # ۲) تارگت‌ها در سمت درست و به ترتیب
    for tp in sig.tps:
        if (sig.side == "BUY" and tp <= ref_for_checks) or (sig.side == "SELL" and tp >= ref_for_checks):
            issues.append(Issue("TP_WRONG_SIDE", FATAL, f"تارگت {tp} در سمت اشتباه ورود است"))
    ordered = sorted(sig.tps, reverse=(sig.side == "SELL"))
    if sig.tps != ordered:
        sig.tps = ordered
        issues.append(Issue("TP_ORDER", INFO, "ترتیب تارگت‌ها اصلاح شد"))

    # ۳) فاصله‌ی ورود از بازار
    # اختلاف قیمت بین بروکرها (طلا 1 تا 3 دلار) از فاصله کم می‌شود تا به‌خاطر آن هشدار ندهیم
    tol = float((v.get("broker_tolerance") or {}).get(sig.symbol, spec.broker_tolerance))
    if market_price and entry is not None and not sig.entry_is_market:
        raw_gap = abs(entry - market_price)
        dist = max(0.0, raw_gap - tol) / market_price * 100
        if dist > float(v.get("max_entry_distance_pct", 3.0)):
            issues.append(Issue("ENTRY_FAR_FROM_MARKET", FATAL if market_independent else REVIEW,
                                f"ورود {entry} با قیمت بازار {market_price} {dist:.2f}٪ فاصله دارد "
                                "(خطای خواندن یا ضبط قدیمی؟)"))
        elif sig.order_type == "MARKET" and dist > float(v.get("market_entry_tolerance_pct", 0.3)):
            issues.append(Issue("MARKET_ENTRY_DRIFT", REVIEW,
                                f"ورود بازار {entry} با قیمت فعلی {market_price} فاصله دارد"))

    # ۴) سازگاری نوع سفارش با جهت و قیمت
    if market_price and entry is not None and sig.order_type in {"LIMIT", "STOP"} and abs(entry - market_price) > tol:
        below = entry < market_price
        expected = "LIMIT" if (sig.side == "BUY") == below else "STOP"
        if expected != sig.order_type:
            if sig.order_type_inferred:
                sig.order_type = expected
            else:
                issues.append(Issue(f"{sig.order_type}_TYPE_MISMATCH", REVIEW,
                                    f"{sig.side} {sig.order_type} در {entry} با قیمت {market_price} سازگار نیست "
                                    f"(احتمالاً {sig.side} {expected})"))

    # ۵) ریسک به ریوارد
    if sig.tps and sig.sl is not None:
        risk = abs(ref_for_checks - sig.sl)
        reward = abs(sig.tps[0] - ref_for_checks)
        lo, hi = v.get("rr_min", 0.2), v.get("rr_max", 20)
        if risk > 0 and not (lo <= reward / risk <= hi):
            issues.append(Issue("RR_ABNORMAL", REVIEW, f"نسبت ریسک به ریوارد 1:{reward / risk:.2f} غیرعادی است"))

    return issues


def route(sig: Signal, issues: list[Issue], cfg: dict[str, Any]) -> str:
    """تصمیم نهایی: publish | review | reject | draft."""
    v = cfg.get("validation", {})
    codes = {i.code for i in issues}
    if sig.intent in SILENT_INTENTS:
        return "reject"
    if "INCOMPLETE" in codes or "NO_SIDE" in codes:
        return "draft"
    if any(i.severity == FATAL for i in issues):
        return "reject"
    if sig.confidence < float(v.get("min_confidence", 0.4)):
        return "reject"
    if any(i.severity == REVIEW for i in issues):
        return "review"
    if sig.confidence < float(v.get("auto_publish_confidence", 0.8)):
        return "review"
    if sig.speaker_is_analyst is False or sig.conflict:
        return "review"
    if sig.intent == "CONDITIONAL" and not v.get("publish_conditional", True):
        return "review"
    return "publish"


def merge_signals(old: Signal, new: Signal) -> Signal:
    """تکمیل سیگنال ناقص با اطلاعات جدید (مثلاً ورود اول، استاپ بعد)."""
    merged = Signal.from_dict(old.to_dict())
    for f in ("side", "order_type", "entry", "entry_low", "entry_high", "sl", "condition", "timeframe",
              "chart_symbol", "drawing_tool"):
        val = getattr(new, f)
        if val is not None:
            setattr(merged, f, val)
    if new.entries:
        merged.entries = new.entries
    if new.tps:
        merged.tps = list(dict.fromkeys(new.tps))
    merged.tp_open = merged.tp_open or new.tp_open
    if new.intent in {"ACTUAL_ENTRY", "PENDING_ORDER"} or merged.intent not in ENTRY_INTENTS:
        merged.intent = new.intent
    merged.confidence = min(old.confidence, new.confidence) if old.confidence and new.confidence else max(old.confidence, new.confidence)
    merged.entry_is_market = new.entry_is_market if new.entry is not None else old.entry_is_market
    merged.evidence = " … ".join(x for x in (old.evidence, new.evidence) if x)
    merged.notes = old.notes + new.notes
    merged.conflict = old.conflict or new.conflict
    merged.pip_converted = old.pip_converted or new.pip_converted
    merged.spoken_at = old.spoken_at or new.spoken_at
    return merged
