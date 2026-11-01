"""تست آفلاین کامل بدون جلسه، دستگاه صوتی، API یا تلگرام واقعی."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.ai import format_signal_message


SAMPLE_TRANSCRIPT = (
    "در جفت ارز EURUSD روند کوتاه مدت صعودی است. قیمت بالای حمایت 1.0840 قرار دارد. "
    "در صورت شکست مقاومت 1.0880، ورود خرید در محدوده 1.0885 با حد ضرر 1.0840 و اهداف 1.0920 و 1.0960 مناسب است."
)

SAMPLE_CHART = (
    "جفت‌ارز: EURUSD\nتایم‌فریم: 15 دقیقه\nروند: صعودی\n"
    "حمایت: 1.0840\nمقاومت: 1.0880\nقیمت در نزدیکی مقاومت و در حال تشکیل کف‌های بالاتر است."
)


class FakeAI:
    async def transcribe(self, wav_path: Path) -> str:
        return SAMPLE_TRANSCRIPT

    async def analyze_chart(self, image_path: Path, context: str = "") -> str:
        return SAMPLE_CHART

    async def generate_analysis(self, transcript: str, chart_analyses: list[str]) -> dict[str, Any]:
        return {
            "summary": "روند کوتاه‌مدت EURUSD صعودی است و حمایت 1.0840 حفظ شده؛ شکست 1.0880 می‌تواند ادامه‌ی حرکت صعودی را تأیید کند.",
            "signal": {
                "pair": "EURUSD", "direction": "buy", "entry": "1.0885",
                "stop_loss": "1.0840", "take_profit": ["1.0920", "1.0960"], "confidence": "medium",
            },
            "opportunity": "",
            "enough_info": True,
        }


class FakePublisher:
    async def publish(self, text: str) -> bool:
        print("\n--- پیام شبیه‌سازی‌شده ---\n" + text + "\n")
        return True


async def run_simulation() -> dict[str, Any]:
    ai = FakeAI()
    result = await ai.generate_analysis(SAMPLE_TRANSCRIPT, [SAMPLE_CHART])
    message = format_signal_message(result)
    await FakePublisher().publish(message)
    output = Path("session_data") / "simulation_result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"input": SAMPLE_TRANSCRIPT, "result": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    asyncio.run(run_simulation())
