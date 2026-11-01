"""لایه‌ی AI: تبدیل گفتار به متن، تحلیل تصویر چارت و تحلیل/سیگنال نهایی.

با Sinox API (سازگار با OpenAI) یا OpenAI مستقیم کار می‌کند.
"""
from __future__ import annotations

import asyncio
import base64
import random
import re
from pathlib import Path
from typing import Any

import httpx

from app.prompts import LIVE_EXTRACTION_SYSTEM, STT_PROMPT
from app.utils import get_logger, now_iso

STT_PROMPT_HINT = STT_PROMPT

# کدهای وضعیتی که گذرایند و ارزش تلاش مجدد دارند
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def encode_image_data_url(img: Any, max_width: int = 1400, quality: int = 92) -> str:
    """کوچک‌سازی + JPEG + base64 از یک تصویر PIL.

    خوانایی اعداد چارت حفظ می‌شود؛ حجم ارسال و زمان پاسخ مدل بینایی کم (بخش ۱۶ مستندات).
    CPU-bound است — فقط داخل asyncio.to_thread صدا زده شود.
    """
    import io

    from PIL import Image

    if img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


class AIProvider:
    def __init__(self, cfg: dict[str, Any]):
        ai = cfg["ai"]
        self.provider = ai.get("provider", "sinox")
        p = ai.get(self.provider, {})
        self.base_url: str = p.get("base_url", "").rstrip("/")
        self.api_key: str = p.get("api_key", "")
        self.stt_model: str = p.get("stt_model", "whisper-1")
        self.vision_model: str = p.get("vision_model", "gpt-4o")
        self.text_model: str = p.get("text_model", "gpt-4o")
        self.max_retries: int = int(p.get("max_retries", 2))
        self.retry_base_sec: float = float(p.get("retry_base_sec", 1.5))
        live = cfg.get("live") or {}
        self.image_max_width: int = int(live.get("vision_image_max_width", 1400))
        # پیش‌فیلتر ارزان: مدل کوچک/سریع که قبل از مدل اصلی می‌پرسد «آیا رویداد تازه‌ای هست؟»
        pf = live.get("vision_prefilter") or {}
        self.precheck_model: str = pf.get("model") or ""
        self.precheck_enabled: bool = bool(pf.get("enabled", False)) and bool(self.precheck_model)
        self.log = get_logger("ai", cfg["logging"]["level"], cfg["logging"]["dir"])
        self._client = httpx.AsyncClient(timeout=180)

    async def _post_with_retry(self, what: str, **kw: Any) -> httpx.Response:
        """POST با تلاش مجدد نمایی برای خطاهای گذرا (شبکه، 429، 5xx).

        خطاهای گذرا نباید فریم/قطعه‌ی صوتی را از دست بدهند؛ فقط خطای پایدار بالا می‌آید.
        """
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.retry_base_sec * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                self.log.warning("تلاش مجدد %d/%d برای %s پس از %.1fs (%s)",
                                 attempt, self.max_retries, what, delay, last)
                await asyncio.sleep(delay)
            try:
                r = await self._client.post(**kw)
                if r.status_code in RETRYABLE_STATUS:
                    retry_after = 0.0
                    if r.status_code == 429:
                        try:
                            retry_after = float((r.json().get("parameters") or {}).get("retry_after", 0))
                        except Exception:  # noqa: BLE001
                            retry_after = 0.0
                    last = httpx.HTTPStatusError(f"HTTP {r.status_code}: {r.text[:200]}",
                                                 request=r.request, response=r)
                    if retry_after:
                        self.retry_base_sec = max(self.retry_base_sec, min(retry_after, 20))
                    continue
                return r
            except httpx.TransportError as e:
                last = e
        assert last is not None
        raise last

    def _image_data_url(self, path: Path) -> str:
        """تصویر روی دیسک را برای ارسال سریع‌تر کوچک و فشرده می‌کند (خوانایی اعداد حفظ می‌شود).

        CPU-bound است — در مسیر async با asyncio.to_thread صدا زده شود.
        """
        from PIL import Image

        try:
            return encode_image_data_url(Image.open(path), self.image_max_width)
        except Exception:  # noqa: BLE001
            b64 = base64.b64encode(path.read_bytes()).decode()
            return f"data:image/png;base64,{b64}"

    # ---------- STT ----------
    async def transcribe(self, wav_path: Path) -> str:
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = None
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.retry_base_sec * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                self.log.warning("تلاش مجدد %d/%d برای STT پس از %.1fs (%s)",
                                 attempt, self.max_retries, delay, last_err)
                await asyncio.sleep(delay)
            try:
                # فایل باید در هر تلاش از نو باز شود (بعد از خواندن کامل به EOF می‌رسد)
                with open(wav_path, "rb") as f:
                    files = {"file": (wav_path.name, f, "audio/wav")}
                    data = {"model": self.stt_model, "language": "fa", "prompt": STT_PROMPT_HINT}
                    r = await self._client.post(url, files=files, data=data, headers=headers)
                if r.status_code in RETRYABLE_STATUS:
                    last_err = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    continue
                break
            except httpx.TransportError as e:
                last_err = e
                r = None
        if r is None:
            assert last_err is not None
            raise last_err
        r.raise_for_status()
        text = r.json().get("text", "").strip()
        # حذف آرتیفکت‌های چینی/ژاپنی/کره‌ای که مدل گاهی روی صدای ضعیف تولید می‌کند
        text = re.sub(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", text).strip()
        if text:
            self.log.info("متن گفتار (%d کاراکتر): %s...", len(text), text[:80])
        return text

    # ---------- Vision ----------
    async def analyze_chart(self, image_path: Path, context: str = "") -> str:
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        payload = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "این تصویر از چارت فارکس در یک جلسه‌ی تحلیل زنده است. "
                                "جفت‌ارز، تایم‌فریم، جهت روند، سطوح مهم (حمایت/مقاومت)، "
                                "الگوها و هر عدد قابل‌مشاهده را دقیق استخراج کن. "
                                f"بافتار گفتار اخیر: {context or '—'}"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
        }
        r = await self._post_with_retry(
            "تحلیل چارت",
            url=f"{self.base_url}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    # ---------- Analysis + Signal ----------
    SIGNAL_SYSTEM = (
        "تو دستیار تحلیل فارکس در جلسات لایو هستی. گفتار (فارسی) تحلیل‌گر و تحلیل تصاویر چارت "
        "به تو داده می‌شود. وظیفه‌ی تو: 1) خلاصه‌ی تحلیل، 2) استخراج سیگنال معاملاتی ساختاریافته. "
        "فقط اگر اطلاعات کافی و صریح وجود داشت سیگنال بده؛ وگرنه صادقانه بگو اطلاعات کافی نیست. "
        "حدس نزن. خروجی را دقیقاً به این JSON بده:\n"
        "{\n"
        '  "summary": "خلاصه‌ی تحلیل به فارسی (۲-۴ جمله)",\n'
        '  "signal": {\n'
        '    "pair": "مثل EURUSD یا null",\n'
        '    "direction": "buy | sell | null",\n'
        '    "entry": "نقطه ورود یا null",\n'
        '    "stop_loss": "حد ضرر یا null",\n'
        '    "take_profit": ["حدود سود ۱", "حدود سود ۲"],\n'
        '    "confidence": "low | medium | high"\n'
        "  },\n"
        '  "opportunity": "اگر سیگنال کامل نیست ولی فرصت مطرح شده، توضیح کوتاه فارسی",\n'
        '  "enough_info": true | false\n'
        "}"
    )

    # ---------- Live signal from chart (event driven) ----------
    CHART_SIGNAL_SYSTEM = (
        "تو یک دستیار بینایی ماشین برای تحلیل چارت فارکس در جلسه‌ی زنده هستی. "
        "تصویر ممکن است شامل خطوط و رسم‌های تحلیلگر باشد: خط ورود، حد ضرر (معمولاً قرمز)، "
        "حدود سود (معمولاً سبز)، مستطیل‌ها، فلش‌ها و برچسب‌های قیمتی. "
        "منبع اعداد: یا در تصویر خوانا دیده می‌شوند، یا تحلیلگر صریحاً در گفتار همزمان گفته است "
        "(اعداد فارسی/گفتاری مثل «سه هزار و نهصد و هشتاد و پنج و نیم» را به عدد تبدیل کن؛ "
        "اگر فقط ارقام آخر گفته شد، با قیمت روی چارت کامل کن). "
        "اگر عدد تصویر و گفتار متناقض بودند، عدد تصویر را بگذار و تناقض را در notes بنویس. "
        "حدس نزن؛ عدد نامطمئن را null بگذار. "
        "خروجی فقط JSON:\n"
        "{\n"
        '  "chart_visible": true|false,\n'
        '  "pair": "EURUSD یا XAUUSD یا null",\n'
        '  "timeframe": "M1/M5/M15/H1 یا null",\n'
        '  "direction": "buy|sell|null",\n'
        '  "entry": "عدد یا null",\n'
        '  "stop_loss": "عدد یا null",\n'
        '  "take_profit": ["عدد", "عدد"],\n'
        '  "annotations": ["شرح کوتاه هر رسم دیده‌شده"],\n'
        '  "confidence": "low|medium|high",\n'
        '  "notes": "اگر نکته‌ای هست"\n'
        "}"
    )

    async def extract_chart_signal(self, image_path: Path, context: str = "") -> dict[str, Any]:
        """استخراج سیگنال/رسم‌های تحلیلگر از یک فریم چارت."""
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        payload = {
            "model": self.vision_model,
            "messages": [
                {"role": "system", "content": self.CHART_SIGNAL_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"گفتار همزمان تحلیلگر: {context or '—'}",
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                },
            ],
        }
        r = await self._post_with_retry(
            "استخراج سیگنال چارت",
            url=f"{self.base_url}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return self._parse_json(content)

    async def precheck_frame(self, image_path: Path) -> bool:
        """پیش‌فیلتر ارزان (اختیاری): آیا در تصویر رویداد/رسم معاملاتی تازه‌ای هست؟

        با مدل کوچکِ `live.vision_prefilter.model` اجرا می‌شود و اگر False بدهد
        مدل اصلیِ گران صدا زده نمی‌شود (صرفه‌جویی چند ثانیه‌ای در هر فریم بی‌رویداد).
        False مثبت (از دست دادن رویداد) ممکن است؛ پیش‌فرض غیرفعال است.
        """
        if not self.precheck_enabled:
            return True
        # کدگذاری JPEG روی event loop بلاک نشود
        image_url = await asyncio.to_thread(self._image_data_url, image_path)
        payload = {
            "model": self.precheck_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "این تصویر از چارت معاملاتی است. آیا رسم معاملاتی تازه‌ای دیده می‌شود؟ "
                                "(خط افقی جدید، ابزار Long/Short Position، فلش، مستطیل، برچسب قیمت ورود/استاپ/هدف). "
                                "تغییر قیمت کندل جاری و حرکت عادی نمودار حساب نمی‌شود. "
                                "فقط JSON: {\"trading_drawing\": true|false}"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }
        try:
            r = await self._post_with_retry(
                "پیش‌فیلتر چارت",
                url=f"{self.base_url}/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            r.raise_for_status()
            data = self._parse_json(r.json()["choices"][0]["message"]["content"] or "")
            return bool(data.get("trading_drawing", True))
        except Exception as e:  # noqa: BLE001
            # پیش‌فیلتر نباید جلوی تحلیل را بگیرد؛ در خطا مدل اصلی صدا زده می‌شود
            self.log.debug("پیش‌فیلتر ناموفق (%s)؛ مدل اصلی ادامه می‌دهد", type(e).__name__)
            return True

    async def extract_live_events(self, image_path: Path | None, user_text: str,
                                  image_url: str | None = None) -> tuple[dict[str, Any], str]:
        """استخراج رویدادهای معاملاتی (نسخه‌ی ۲). خروجی: (JSON تجزیه‌شده، متن خام پاسخ).

        image_url: اگر از قبل آماده شده (asyncio.to_thread در ChartWatcher)، از همان استفاده
        می‌شود تا تصویر دوباره از دیسک بازخوانی و کدگذاری نشود.
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        if image_url or image_path is not None:
            url = image_url or self._image_data_url(image_path)
            content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
        payload = {
            "model": self.vision_model,
            "messages": [
                {"role": "system", "content": LIVE_EXTRACTION_SYSTEM},
                {"role": "user", "content": content},
            ],
        }
        r = await self._post_with_retry(
            "استخراج رویداد لایو",
            url=f"{self.base_url}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"] or ""
        data = self._parse_json(raw)
        if data.get("parse_error"):
            data = {"frame": {"type": "other"}, "events": [], "parse_error": True}
        data.setdefault("events", [])
        data.setdefault("frame", {})
        return data, raw

    async def generate_analysis(self, transcript: str, chart_analyses: list[str]) -> dict[str, Any]:
        user_msg = (
            f"### گفتار تحلیل‌گر (متن فارسی):\n{transcript or '—'}\n\n"
            f"### تحلیل تصاویر چارت:\n" + "\n---\n".join(chart_analyses or ["—"])
        )
        payload = {
            "model": self.text_model,
            "messages": [
                {"role": "system", "content": self.SIGNAL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        }
        r = await self._post_with_retry(
            "خلاصه‌ی دوره‌ای",
            url=f"{self.base_url}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return self._parse_json(content)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        import json
        import re

        content = content.strip()
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            content = m.group(0)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"summary": content, "signal": None, "enough_info": False, "parse_error": True}


def format_signal_message(result: dict[str, Any]) -> str:
    """ساخت پیام نهایی فارسی برای ارسال به کانال."""
    lines: list[str] = []
    sig = result.get("signal") or {}
    pair = sig.get("pair")
    direction = sig.get("direction")
    emoji = {"buy": "🟢 خرید", "sell": "🔴 فروش"}.get(direction)

    if pair and direction and emoji:
        lines.append(f"⚡ سیگنال {pair} — {emoji}")
        if sig.get("entry"):
            lines.append(f"🎯 ورود: {sig['entry']}")
        if sig.get("stop_loss"):
            lines.append(f"🛑 حد ضرر: {sig['stop_loss']}")
        tps = sig.get("take_profit") or []
        if tps:
            lines.append("💰 اهداف: " + " | ".join(str(tp) for tp in tps if tp))
        conf = {"high": "بالا", "medium": "متوسط", "low": "پایین"}.get(sig.get("confidence"))
        if conf:
            lines.append(f"📊 اطمینان: {conf}")
    elif result.get("opportunity") and result.get("enough_info") is not False:
        lines.append(f"👀 فرصت احتمالی: {result['opportunity']}")
    else:
        lines.append("🔎 فرصت مشخصی در این بخش از جلسه مطرح نشد.")

    if result.get("summary"):
        lines.append("")
        lines.append(f"📝 خلاصه تحلیل: {result['summary']}")
    lines.append("")
    lines.append(f"🕒 {now_iso()} — دستیار تحلیل جلسات لایو")
    return "\n".join(lines)
