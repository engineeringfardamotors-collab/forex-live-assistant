"""تست سرتاسری واقعی با API: چارت ساختگی + گفتار واقعی → تحلیل و سیگنال.

اجرا:
    python -m app.e2e_real
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

from PIL import Image, ImageDraw

TRANSCRIPT = (
    "یورو دلار در تایم فریم پانزده دقیقه روند صعودی دارد. قیمت بالای حمایت یک و صفر و هشتاد و چهار سنت "
    "تثبیت شده است. اگر مقاومت یک و صفر و هشتاد و هشت شکسته شود، ورود خرید در محدوده یک و صفر و هشتاد و هشت "
    "با حد ضرر یک و صفر و هشتاد و چهار و اهداف یک و صفر و نه و بیست و یک و صفر و نه و شصت پیشنهاد می‌شود."
)


def make_chart(path: Path) -> None:
    """ساخت یک چارت کندلی ساختگی با سطوح حمایت/مقاومت."""
    path.parent.mkdir(parents=True, exist_ok=True)
    W, H = 1100, 640
    img = Image.new("RGB", (W, H), "#0b1424")
    d = ImageDraw.Draw(img)

    # شبکه
    for y in range(80, H - 60, 60):
        d.line([(70, y), (W - 40, y)], fill="#16233a")
    for x in range(70, W - 40, 90):
        d.line([(x, 60), (x, H - 60)], fill="#16233a")

    # قیمت‌ها: 1.0800 تا 1.0960
    lo, hi = 1.0800, 1.0960

    def py(price: float) -> float:
        return H - 60 - (price - lo) / (hi - lo) * (H - 140)

    random.seed(7)
    price = 1.0845
    x = 80
    while x < W - 60:
        drift = random.uniform(-0.0009, 0.0011)
        o = price
        c = max(lo + 0.0002, min(hi - 0.0002, price + drift))
        h = min(hi - 0.0001, max(o, c) + random.uniform(0.0001, 0.0005))
        l = max(lo + 0.0001, min(o, c) - random.uniform(0.0001, 0.0005))
        color = "#26c281" if c >= o else "#e0506b"
        d.line([(x + 8, py(h)), (x + 8, py(l))], fill=color, width=2)
        top, bot = py(max(o, c)), py(min(o, c))
        d.rectangle([(x, top), (x + 16, bot if bot > top else top + 2)], fill=color)
        price = c
        x += 22

    # سطوح کلیدی
    d.line([(70, py(1.0880)), (W - 40, py(1.0880))], fill="#ff5d73", width=3)
    d.text((W - 240, py(1.0880) - 22), "RESISTANCE 1.0880", fill="#ff5d73")
    d.line([(70, py(1.0840)), (W - 40, py(1.0840))], fill="#3ea6ff", width=3)
    d.text((W - 240, py(1.0840) + 8), "SUPPORT 1.0840", fill="#3ea6ff")

    d.text((80, 24), "EURUSD  -  M15", fill="#eaf2ff")
    d.text((80, 44), "Live session chart (simulated)", fill="#7f9cc0")
    img.save(path, "PNG")


async def main() -> None:
    from app.ai import AIProvider, format_signal_message
    from app.config import load_config

    cfg = load_config()
    chart_path = Path("session_data") / "e2e" / "chart.png"
    make_chart(chart_path)

    ai = AIProvider(cfg)
    print("=== ۱) تحلیل تصویر چارت ===")
    chart = await ai.analyze_chart(chart_path, "EURUSD M15")
    print(chart[:700])

    print("\n=== ۲) تحلیل ترکیبی و تولید سیگنال ===")
    result = await ai.generate_analysis(TRANSCRIPT, [chart])
    print("\n--- پیام نهایی برای تلگرام ---")
    print(format_signal_message(result))


if __name__ == "__main__":
    asyncio.run(main())
