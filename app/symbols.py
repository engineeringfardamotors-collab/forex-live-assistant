"""نمادها و اعداد: نام استاندارد، تعداد اعشار، اندازه‌ی پیپ و تکمیل عددهای خلاصه‌شده.

قواعد:
- هر نام بروکری/فارسی (GOLD، XAUUSDm، «طلا»، «انس») به یک نام استاندارد نگاشت می‌شود.
- عدد گفتاری خلاصه («۸۵» به‌جای 3985، «پنجاه» به‌جای 1.0850) با قیمت مرجع کامل می‌شود.
- ارقام فارسی/عربی و جداکننده‌ها به قالب استاندارد تبدیل می‌شوند.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int                 # تعداد اعشار نمایش قیمت
    pip_size: float             # اندازه‌ی یک پیپ به واحد قیمت
    short_unit: float           # واحد عدد خلاصه‌ی گفتاری (طلا: «۸۵» → 85 دلار؛ یورو: «۵۰» → 50 پیپ)
    max_sl_pct: float           # حداکثر فاصله‌ی منطقی حد ضرر از ورود (درصد قیمت)
    kind: str = "forex"         # forex | metal | index | crypto | energy
    pip_ambiguous: bool = False  # تعریف پیپ بین تحلیلگرها متفاوت است (مثل طلا)
    broker_tolerance: float = 0.0  # اختلاف قیمت عادی بین بروکرها (واحد قیمت)؛ کمتر از این «اختلاف» حساب نمی‌شود


_FX = dict(digits=5, pip_size=0.0001, short_unit=0.0001, max_sl_pct=1.5, kind="forex", broker_tolerance=0.0003)
_JPY = dict(digits=3, pip_size=0.01, short_unit=0.01, max_sl_pct=1.5, kind="forex", broker_tolerance=0.03)

SPECS: dict[str, SymbolSpec] = {
    # طلا: اختلاف 1 تا 3 دلاری بین بروکر تحلیلگر و MT5 شما عادی است
    "XAUUSD": SymbolSpec("XAUUSD", 2, 0.1, 1.0, 2.5, "metal", pip_ambiguous=True, broker_tolerance=3.0),
    "XAGUSD": SymbolSpec("XAGUSD", 3, 0.01, 0.01, 4.0, "metal", pip_ambiguous=True),
    "US30": SymbolSpec("US30", 1, 1.0, 1.0, 3.0, "index"),
    "NAS100": SymbolSpec("NAS100", 1, 1.0, 1.0, 3.0, "index"),
    "US500": SymbolSpec("US500", 1, 0.1, 1.0, 3.0, "index"),
    "GER40": SymbolSpec("GER40", 1, 1.0, 1.0, 3.0, "index"),
    "BTCUSD": SymbolSpec("BTCUSD", 2, 1.0, 1.0, 10.0, "crypto"),
    "ETHUSD": SymbolSpec("ETHUSD", 2, 0.1, 1.0, 10.0, "crypto"),
    "USOIL": SymbolSpec("USOIL", 2, 0.01, 0.01, 5.0, "energy"),
    "UKOIL": SymbolSpec("UKOIL", 2, 0.01, 0.01, 5.0, "energy"),
}
for _p in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD", "EURGBP", "EURCHF",
           "EURAUD", "EURCAD", "GBPAUD", "GBPCAD", "GBPCHF", "AUDCAD", "AUDNZD", "AUDCHF",
           "NZDCAD", "CADCHF", "EURNZD", "GBPNZD"):
    SPECS[_p] = SymbolSpec(_p, **_FX)
for _p in ("USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY"):
    SPECS[_p] = SymbolSpec(_p, **_JPY)

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "TRY", "ZAR", "MXN", "SGD"}

# نام‌های لاتین جایگزین (پس از حذف نویسه‌های غیرحرفی و بزرگ‌کردن)
LATIN_ALIASES: dict[str, str] = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSDT": "XAUUSD", "GOLDUSD": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD",
    "DJ30": "US30", "DJI": "US30", "DOW": "US30", "DOWJONES": "US30", "WS30": "US30", "USA30": "US30", "YM": "US30",
    "NAS": "NAS100", "NASDAQ": "NAS100", "USTEC": "NAS100", "NQ100": "NAS100", "NDX": "NAS100",
    "US100": "NAS100", "USTECH": "NAS100", "NQ": "NAS100",
    "SPX": "US500", "SPX500": "US500", "SP500": "US500", "USA500": "US500",
    "DAX": "GER40", "DE40": "GER40", "GER30": "GER40", "DE30": "GER40",
    "BTC": "BTCUSD", "BTCUSDT": "BTCUSD", "XBTUSD": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETH": "ETHUSD", "ETHUSDT": "ETHUSD",
    "WTI": "USOIL", "XTIUSD": "USOIL", "CL": "USOIL", "OIL": "USOIL",
    "BRENT": "UKOIL", "XBRUSD": "UKOIL",
}

# نام‌های فارسی (پس از نرمال‌سازی ی/ک و حذف نیم‌فاصله)؛ ترتیب: عبارت‌های طولانی‌تر اول
PERSIAN_ALIASES: list[tuple[str, str]] = [
    ("یورو ین", "EURJPY"), ("پوند ین", "GBPJPY"), ("استرالیا ین", "AUDJPY"),
    ("یورو پوند", "EURGBP"), ("یورو دلار", "EURUSD"), ("یورو به دلار", "EURUSD"),
    ("پوند دلار", "GBPUSD"), ("پوند به دلار", "GBPUSD"),
    ("دلار ین", "USDJPY"), ("دلار به ین", "USDJPY"), ("دلار فرانک", "USDCHF"),
    ("دلار کانادا", "USDCAD"), ("دلار کاناد", "USDCAD"),
    ("استرالیا دلار", "AUDUSD"), ("دلار استرالیا", "AUDUSD"), ("اوسی", "AUDUSD"),
    ("نیوزلند", "NZDUSD"), ("کیوی", "NZDUSD"),
    ("داو جونز", "US30"), ("داوجونز", "US30"), ("نزدک", "NAS100"), ("اس اند پی", "US500"),
    ("داکس", "GER40"), ("بیت کوین", "BTCUSD"), ("بیتکوین", "BTCUSD"), ("اتریوم", "ETHUSD"),
    ("نفت برنت", "UKOIL"), ("برنت", "UKOIL"), ("نفت", "USOIL"),
    ("اونس طلا", "XAUUSD"), ("انس طلا", "XAUUSD"), ("طلا", "XAUUSD"), ("انس", "XAUUSD"),
    ("اونس", "XAUUSD"), ("گلد", "XAUUSD"), ("نقره", "XAGUSD"),
    ("یورو", "EURUSD"), ("پوند", "GBPUSD"), ("کابل", "GBPUSD"),
]

_BROKER_SUFFIXES = ("MICRO", "MINI", "CASH", "SPOT", "PRO", "ECN", "RAW", "STD", "VIP", "FX", "M", "I", "C", "Z", "B", "E", "X", "R")

# نام‌های رایج نماد در MT5 بروکرهای مختلف (به ترتیب امتحان)
BROKER_CANDIDATES: dict[str, list[str]] = {
    "XAUUSD": ["XAUUSD", "XAUUSDm", "XAUUSD.", "XAUUSD.m", "XAUUSD+", "XAUUSD.pro", "XAUUSD.ecn", "XAUUSD_i",
               "XAUUSDc", "XAUUSD.r", "GOLD", "GOLDm", "GOLD.", "XAUUSD.vip", "XAUUSD-ECN"],
    "XAGUSD": ["XAGUSD", "XAGUSDm", "XAGUSD.", "SILVER"],
    "US30": ["US30", "US30m", "DJ30", "WS30", "US30.cash", "USA30"],
    "NAS100": ["NAS100", "USTEC", "US100", "NAS100m", "US100.cash", "USTECm"],
    "US500": ["US500", "SPX500", "US500.cash", "US500m"],
    "BTCUSD": ["BTCUSD", "BTCUSDm", "BTCUSD."],
}


def broker_candidates(symbol: str, known: str | None = None) -> list[str]:
    base = [known] if known else []
    base += BROKER_CANDIDATES.get(symbol, [])
    base += [symbol, symbol + "m", symbol + ".", symbol + ".m", symbol + "+", symbol + ".pro", symbol + "c"]
    seen: list[str] = []
    for b in base:
        if b and b not in seen:
            seen.append(b)
    return seen


_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_persian(text: str) -> str:
    """یکسان‌سازی حروف عربی/فارسی، نیم‌فاصله و ارقام."""
    if not text:
        return ""
    t = text.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک").replace("ۀ", "ه").replace("ة", "ه")
    t = t.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    t = t.translate(_DIGIT_MAP).replace("٫", ".").replace("٬", ",")
    return re.sub(r"\s+", " ", t).strip()


def normalize_symbol(raw: Any, overrides: dict[str, str] | None = None) -> str | None:
    """تبدیل هر شکل نام نماد به نام استاندارد؛ None اگر شناخته نشد."""
    if not raw:
        return None
    text = normalize_persian(str(raw))
    if overrides:
        for k, v in overrides.items():
            if k.lower() == text.lower():
                return v
    s = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if s:
        cand = _match_latin(s)
        if cand:
            return cand
    for alias, canon in PERSIAN_ALIASES:
        if alias in text:
            return canon
    return None


def _match_latin(s: str) -> str | None:
    tried = s
    for _ in range(3):
        if tried in SPECS:
            return tried
        if tried in LATIN_ALIASES:
            return LATIN_ALIASES[tried]
        if re.fullmatch(r"[A-Z]{6}", tried) and tried[:3] in CURRENCIES and tried[3:] in CURRENCIES:
            return tried
        stripped = False
        for suf in _BROKER_SUFFIXES:
            if tried.endswith(suf) and len(tried) - len(suf) >= 3:
                tried = tried[: -len(suf)]
                stripped = True
                break
        if not stripped:
            break
    # پیشوند استاندارد درون رشته (مثلاً XAUUSD در «FXXAUUSD»)
    for name in SPECS:
        if name in s:
            return name
    return None


def get_spec(symbol: str | None, overrides: dict[str, dict] | None = None) -> SymbolSpec:
    """مشخصات نماد؛ برای نماد ناشناخته، مشخصات عمومی فارکس/ین."""
    sym = (symbol or "").upper()
    spec = SPECS.get(sym)
    if spec is None:
        if sym.endswith("JPY"):
            spec = SymbolSpec(sym, **_JPY)
        else:
            spec = SymbolSpec(sym or "UNKNOWN", **_FX)
    if overrides and sym in overrides:
        o = overrides[sym] or {}
        spec = replace(spec, **{k: v for k, v in o.items() if k in spec.__dataclass_fields__})
    return spec


def to_float(value: Any) -> float | None:
    """تبدیل عدد با ارقام فارسی، جداکننده‌ی هزارگان و متن اضافه به float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    s = normalize_persian(str(value)).replace(" ", "")
    if not s or s.lower() in {"null", "none", "-", "—"}:
        return None
    # 3,985.50 → 3985.50 ؛ 1,0850 (اعشار اروپایی) → 1.0850
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if all(len(p) == 3 for p in parts[1:]):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def is_plausible(value: float, ref: float) -> bool:
    return ref > 0 and 0.5 * ref <= value <= 2.0 * ref


def expand_short_price(spoken: float, ref_price: float | None, spec: SymbolSpec,
                       max_dev_pct: float = 2.0, above: float | None = None,
                       below: float | None = None) -> float | None:
    """تکمیل عدد خلاصه‌ی گفتاری با قیمت مرجع.

    طلا با قیمت 3982.4:  85 → 3985 ؛ 85.5 → 3985.5 ؛ 10 → 4010 (نزدیک‌ترین)
    یورو با قیمت 1.0832: 50 → 1.0850
    اگر عدد کامل است، همان برگردانده می‌شود. اگر نتیجه بیش از max_dev_pct از
    قیمت مرجع فاصله داشت، None (یعنی مطمئن نیستیم).
    """
    if spoken is None:
        return None
    if ref_price is None or ref_price <= 0:
        return spoken
    if is_plausible(spoken, ref_price):
        return round(spoken, spec.digits)
    if spoken <= 0:
        return None
    int_len = len(str(int(spoken))) if spoken >= 1 else 1
    base = (10 ** int_len) * spec.short_unit
    value = spoken * spec.short_unit
    anchor = math.floor(ref_price / base) * base
    candidates = [anchor + k * base + value for k in (-2, -1, 0, 1, 2)]
    # جهت معلوم (مثلاً تارگت خرید باید بالای ورود باشد): فقط کاندیدهای سمت درست
    directed = [c for c in candidates if (above is None or c > above) and (below is None or c < below)]
    if directed:
        candidates = directed
    best = min(candidates, key=lambda c: abs(c - ref_price))
    if abs(best - ref_price) / ref_price * 100 > max_dev_pct:
        return None
    return round(best, spec.digits)


def pips_to_distance(pips: float, spec: SymbolSpec) -> float:
    return abs(pips) * spec.pip_size


def fmt_price(value: float | None, spec: SymbolSpec) -> str:
    if value is None:
        return "—"
    return f"{value:.{spec.digits}f}"
