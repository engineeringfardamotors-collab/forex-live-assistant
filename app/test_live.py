"""تست آفلاین پایش زنده: چارت مصنوعی با خطوط ورود/استاپ/تی‌پی → استخراج سیگنال.

اجرا:  python -m app.test_live
پیام به کانال ارسال نمی‌شود (ناشر ساختگی).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.ai import AIProvider
from app.config import get_config
from app.live_watcher import ChartWatcher
from app.publisher import MemoryPublisher
from app.trade_state import TradeBook


def make_chart(path: Path) -> Image.Image:
    w, h = 1600, 900
    img = Image.new("RGB", (w, h), (19, 23, 34))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
        big = ImageFont.truetype("arial.ttf", 30)
    except OSError:
        font = big = ImageFont.load_default()
    d.text((20, 15), "XAUUSD, 15  -  Gold Spot / U.S. Dollar", fill=(220, 220, 220), font=big)
    import random
    random.seed(3)
    price, x = 3970.0, 60
    for _ in range(110):
        o = price
        c = o + random.uniform(-3, 3.4)
        hi, lo = max(o, c) + random.uniform(0, 1.5), min(o, c) - random.uniform(0, 1.5)
        y = lambda p: int(h - (p - 3950) * 12)  # noqa: E731
        col = (38, 166, 154) if c >= o else (239, 83, 80)
        d.line([(x + 4, y(hi)), (x + 4, y(lo))], fill=col)
        d.rectangle([x, y(max(o, c)), x + 8, y(min(o, c)) + 1], fill=col)
        price, x = c, x + 12
    y = lambda p: int(h - (p - 3950) * 12)  # noqa: E731
    for p, label, col in [
        (4005.0, "TP2 4005.00", (0, 200, 90)),
        (3995.0, "TP1 3995.00", (0, 200, 90)),
        (3985.5, "BUY ENTRY 3985.50", (80, 160, 255)),
        (3978.0, "SL 3978.00", (255, 60, 60)),
    ]:
        d.line([(900, y(p)), (1340, y(p))], fill=col, width=3)
        d.rectangle([1345, y(p) - 16, 1595, y(p) + 16], fill=col)
        d.text((1352, y(p) - 12), label, fill=(255, 255, 255), font=font)
    img.save(path)
    return img


async def main() -> None:
    cfg = get_config()
    cfg = {**cfg, "recording": {"enabled": False}}
    out = Path("session_data/test_live")
    out.mkdir(parents=True, exist_ok=True)
    img = make_chart(out / "synthetic_chart.png")

    ai = AIProvider(cfg)
    pub = MemoryPublisher(cfg)
    w = ChartWatcher(cfg, ai, pub, out, trades=TradeBook(cfg, pub))
    w._running = True
    w.add_transcript("خب دوستان روی طلا همین الان یک پوزیشن بای لیمیت می‌گیریم، ورود ۳۹۸۵ و نیم، "
                     "استاپ ۳۹۷۸، تی پی یک ۳۹۹۵ تی پی دو ۴۰۰۵")

    print("1) فریم + گفتار → مدل بینایی (دستورالعمل کامل) ...")
    await w.process(img, "chart")
    print("2) همان فریم دوباره (باید تکراری باشد) ...")
    w._last_vision = 0
    await w.process(img, "chart")
    print("3) گفتار فرضی (نباید ارسال شود) ...")
    w.add_transcript("ولی اگه فرضاً قیمت بیاد ۳۹۶۰ اونجا می‌شد فروش گرفت، فعلاً کاری نکنید")
    w._last_vision = 0
    await w.process(img, "speech")

    print("\nآمار:", w.stats())
    for m in pub.sent:
        print(f"[{m['target']}] reply_to={m.get('reply_to')} edit_of={m.get('edit_of')}")
        print(m["text"])
        print("-" * 40)


if __name__ == "__main__":
    asyncio.run(main())
