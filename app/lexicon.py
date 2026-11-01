"""واژه‌نامه‌ی اصطلاحات لایوهای فارسی، اصلاح خطاهای رایج STT و نشانه‌های زبانی.

این ماژول دو کار دارد:
1. نرمال‌سازی متن گفتار قبل از ارسال به مدل (اعداد حرفی → رقم، «سر لیمیت» → «سل لیمیت»).
2. «تور ایمنی»: اگر مدل چیزی را ورود قطعی دانست ولی در جمله‌ی مربوط نفی/فرضیه
   دیده می‌شود، سیگنال به‌جای کانال به صف بازبینی می‌رود.
"""
from __future__ import annotations

import re

from app.symbols import normalize_persian

LEXICON: dict[str, list[str]] = {
    "BUY": ["بای", "خرید", "بخرید", "بخر", "لانگ", "long", "buy"],
    "SELL": ["سل", "فروش", "بفروشید", "بفروش", "شورت", "short", "sell"],
    "ENTRY": ["ورود", "انتری", "entry", "نقطه ورود", "وارد شید", "وارد بشید", "وارد شوید", "پوزیشن"],
    "SL": ["استاپ", "حد ضرر", "اس ال", "sl", "stop"],
    "TP": ["تی پی", "تارگت", "حد سود", "هدف", "tp", "target"],
    "LIMIT": ["لیمیت", "limit"],
    "BE": ["ریسک فری", "بریک ایون", "سربه سر", "سر به سر", "break even"],
    "PARTIAL": ["سیو سود", "سیو", "نصفش رو ببندید", "پارشیال", "درصد ببندید", "درصدش رو ببندید"],
    "CLOSE": ["ببندید", "ببندیم", "خارج شید", "خارج بشید", "کلوز", "close"],
    "CANCEL": ["کنسل", "پاک کنید", "فراموش کنید", "لغو", "حذف کنید"],
    "NEGATION": ["نخرید", "نفروشید", "نگیرید", "وارد نشید", "وارد نشوید", "نزنید", "نکنید", "نباید"],
    "HEDGE": ["اگه", "اگر", "شاید", "احتمالا", "فرض کنید", "مثلا", "فرضا", "ممکنه", "در صورتی که"],
    "PAST": ["دیروز", "هفته پیش", "هفته قبل", "قبلا", "اون روز", "پریروز", "گرفته بودیم", "خریده بودیم"],
}

VERB_FORMS = ["بخریم", "خریدیم", "خریدم", "می خریم", "میخریم", "بفروشیم", "فروختیم", "فروختم", "بگیریم",
              "بگیرید", "گرفتیم", "گرفتم", "میگیریم", "می گیریم", "وارد شدیم", "وارد میشیم"]
ACTION_WORDS = LEXICON["BUY"] + LEXICON["SELL"] + LEXICON["ENTRY"] + LEXICON["NEGATION"] + VERB_FORMS

# اصلاح خطاهای رایج STT؛ فقط در بافت معاملاتی تا معنای جمله‌های عادی تغییر نکند
STT_FIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:سر|سال|سِل|ثل)\s+(?=(?:لیمیت|استاپ|بگیرید|بزنید|کنید|بدید|میگیریم|می گیریم))"), "سل "),
    (re.compile(r"\bبای\s*بای\s+(?=(?:لیمیت|استاپ|بگیرید|بزنید))"), "بای "),
    (re.compile(r"\bتی\s*[‌\-]?\s*پی"), "تی پی"),
    (re.compile(r"\bتیپی\b"), "تی پی"),
    (re.compile(r"\bاستاپ\s*لاس"), "استاپ"),
    (re.compile(r"\bاستاپلاس"), "استاپ"),
    (re.compile(r"\bریسک\s*فری"), "ریسک فری"),
    (re.compile(r"\bبای\s*لیمیت"), "بای لیمیت"),
    (re.compile(r"\bسل\s*لیمیت"), "سل لیمیت"),
    (re.compile(r"\bبای\s*استاپ"), "بای استاپ"),
    (re.compile(r"\bسل\s*استاپ"), "سل استاپ"),
]

_UNITS = {
    "صفر": 0, "یک": 1, "یه": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6,
    "هفت": 7, "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13,
    "چهارده": 14, "پانزده": 15, "پونزده": 15, "شانزده": 16, "شونزده": 16, "هفده": 17,
    "هیفده": 17, "هجده": 18, "هیجده": 18, "نوزده": 19,
}
_TENS = {"بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90}
_HUNDREDS = {
    "صد": 100, "یکصد": 100, "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500, "پونصد": 500,
    "ششصد": 600, "شیشصد": 600, "هفتصد": 700, "هشتصد": 800, "نهصد": 900,
}
_SCALES = {"هزار": 1000, "میلیون": 1_000_000}
_FRACTIONS = {"نیم": 0.5, "ربع": 0.25}
_NUM_WORDS = set(_UNITS) | set(_TENS) | set(_HUNDREDS) | set(_SCALES) | set(_FRACTIONS)


def _parse_number_words(tokens: list[str]) -> float | None:
    total, current, frac, seen = 0, 0, 0.0, False
    for tok in tokens:
        if tok == "و":
            continue
        if tok in _UNITS:
            current += _UNITS[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok in _HUNDREDS:
            current += _HUNDREDS[tok]
        elif tok in _SCALES:
            current = max(current, 1) * _SCALES[tok]
            total += current
            current = 0
        elif tok in _FRACTIONS:
            frac += _FRACTIONS[tok]
        else:
            return None
        seen = True
    if not seen:
        return None
    return total + current + frac


def words_to_numbers(text: str) -> str:
    """«سه هزار و نهصد و هشتاد و پنج و نیم» → 3985.5 ؛ «هشتاد و پنج» → 85.

    «نه» تنها (به معنی «خیر») تبدیل نمی‌شود. عدد رقمی + «و نیم» هم پشتیبانی می‌شود:
    «۸۵ و نیم» → 85.5
    """
    tokens = text.split(" ")
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in _NUM_WORDS and tokens[i] not in _FRACTIONS:
            j = i
            span: list[str] = []
            while j < len(tokens) and (tokens[j] in _NUM_WORDS or (tokens[j] == "و" and j + 1 < len(tokens) and tokens[j + 1] in _NUM_WORDS)):
                span.append(tokens[j])
                j += 1
            if span == ["نه"]:
                out.append(tokens[i])
                i += 1
                continue
            value = _parse_number_words(span)
            if value is not None:
                out.append(_fmt_num(value))
                i = j
                continue
        out.append(tokens[i])
        i += 1
    result = " ".join(out)
    result = re.sub(r"(\d+(?:\.\d+)?)\s+و\s+نیم\b", lambda m: _fmt_num(float(m.group(1)) + 0.5), result)
    result = re.sub(r"(\d+(?:\.\d+)?)\s+و\s+ربع\b", lambda m: _fmt_num(float(m.group(1)) + 0.25), result)
    return result


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def normalize_trading_text(text: str) -> str:
    t = normalize_persian(text)
    for pat, rep in STT_FIXES:
        t = pat.sub(rep, t)
    return words_to_numbers(t)


def _contains(text: str, words: list[str]) -> bool:
    low = f" {text.lower()} "
    return any(re.search(rf"(?<![\w]){re.escape(w.lower())}(?![\w])", low) for w in words)


def has_trading_terms(text: str) -> bool:
    t = normalize_persian(text)
    groups = ("BUY", "SELL", "ENTRY", "SL", "TP", "BE", "PARTIAL", "CLOSE", "CANCEL", "LIMIT")
    return any(_contains(t, LEXICON[g]) for g in groups)


def is_trigger(text: str) -> bool:
    """گفتار حاوی اعلام/مدیریت معامله است → بررسی فوری چارت."""
    return has_trading_terms(text)


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[.!?؟،,;؛\n]| ولی | اما ", text) if c.strip()]


def speech_flags(text: str) -> set[str]:
    """نشانه‌های زبانی در جمله‌هایی که حاوی فعل معاملاتی‌اند."""
    flags: set[str] = set()
    for clause in _clauses(normalize_persian(text)):
        if not _contains(clause, ACTION_WORDS + LEXICON["CLOSE"] + LEXICON["SL"] + LEXICON["TP"]):
            continue
        if _contains(clause, LEXICON["NEGATION"]):
            flags.add("NEGATION")
        if _contains(clause, LEXICON["HEDGE"]):
            flags.add("HEDGE")
        if _contains(clause, LEXICON["PAST"]):
            flags.add("PAST")
    return flags


def merge_overlap(prev: str, new: str, min_words: int = 2, max_words: int = 12) -> str:
    """حذف بخش تکراری ابتدای new که با انتهای prev یکی است (قطعه‌های صوتی همپوشان)."""
    if not prev or not new:
        return new
    pw, nw = prev.split(), new.split()
    for k in range(min(max_words, len(pw), len(nw)), min_words - 1, -1):
        a = [normalize_persian(x).strip(".,،!؟?") for x in pw[-k:]]
        b = [normalize_persian(x).strip(".,،!؟?") for x in nw[:k]]
        if a == b:
            return " ".join(nw[k:])
    return new
