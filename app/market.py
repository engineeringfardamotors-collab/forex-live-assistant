"""قیمت بازار از منبع مستقل + وضعیت باز/بسته بودن بازار.

ترتیب منابع (قابل تنظیم در config → market.sources):
  mt5   : ترمینال MetaTrader 5 روی همین سیستم (pip install MetaTrader5)
  http  : یک API قیمت با الگوی URL و مسیر JSON
  chart : آخرین قیمتی که مدل از برچسب قیمت نمودار خوانده (مستقل نیست)

فقط منابع mt5/http «مستقل» حساب می‌شوند؛ اختلاف شدید با آن‌ها خطای قطعی است
(نشانه‌ی خطای خواندن عدد یا پخش ضبط جلسه‌ی قدیمی).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.symbols import broker_candidates, get_spec, is_plausible, normalize_symbol
from app.utils import get_logger

# پیش‌تنظیم‌های آماده‌ی منبع HTTP (بدون کلید) — با market.http.preset فعال می‌شوند
HTTP_PRESETS: dict[str, dict[str, Any]] = {
    "gold-api": {   # رایگان و بدون کلید: فلزات گران‌بها
        "url": "https://api.gold-api.com/price/{symbol}",
        "json_path": "price",
        "symbol_map": {"XAUUSD": "XAU", "XAGUSD": "XAG", "XPTUSD": "XPT", "XPDUSD": "XPD"},
    },
}


class MarketPriceProvider:
    def __init__(self, cfg: dict[str, Any]):
        m = cfg.get("market", {})
        self.log = get_logger("market", cfg["logging"]["level"], cfg["logging"]["dir"])
        self.sources: list[str] = list(m.get("sources", ["mt5", "chart"]))
        self.broker_symbols: dict[str, str] = m.get("broker_symbols", {}) or {}
        self.http_cfg: dict[str, Any] = dict(m.get("http", {}) or {})
        preset = str(self.http_cfg.get("preset") or "").lower()
        if preset and not self.http_cfg.get("url"):
            if preset in HTTP_PRESETS:
                base = HTTP_PRESETS[preset]
                merged = dict(base)
                # نگاشت نماد کاربر روی نگاشت پیش‌فرض preset ادغام می‌شود (نه جایگزینی کامل)
                merged["symbol_map"] = {**base.get("symbol_map", {}), **(self.http_cfg.get("symbol_map") or {})}
                for k, v in self.http_cfg.items():
                    if k not in {"preset", "symbol_map"} and v not in (None, {}, []):
                        merged[k] = v
                self.http_cfg = merged
                self.log.info("منبع قیمت HTTP با پیش‌تنظیم «%s» فعال شد", preset)
            else:
                self.log.warning("پیش‌تنظیم HTTP ناشناخته: %s (پیش‌تنظیم‌ها: %s)",
                                 preset, ", ".join(HTTP_PRESETS))
        self.chart_max_age = float(m.get("chart_price_max_age_sec", 120))
        self.cache_sec = float(m.get("cache_sec", 2))
        self._chart: dict[str, tuple[float, float]] = {}
        self._cache: dict[str, tuple[float, float, str]] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._mt5 = None
        self._mt5_failed = False
        self._mt5_retry_at = 0.0
        self._resolved: dict[str, str | None] = {}
        self.mt5_path = (m.get("mt5_path") or "").strip()
        self.mt5_max_tick_age = float(m.get("mt5_max_tick_age_sec", 300))
        self.mt5_state = "هنوز امتحان نشده" if "mt5" in self.sources else "غیرفعال"

    # ---------- chart (from vision) ----------
    def update_from_chart(self, symbol: str | None, price: float | None) -> None:
        if symbol and price:
            prev = self._chart.get(symbol)
            if prev and not is_plausible(price, prev[0]):
                self.log.warning("قیمت خوانده‌شده از نمودار %s=%s با قبلی %s ناسازگار است؛ نادیده گرفته شد",
                                 symbol, price, prev[0])
                return
            self._chart[symbol] = (price, time.time())

    def chart_price(self, symbol: str) -> float | None:
        item = self._chart.get(symbol)
        if item and time.time() - item[1] <= self.chart_max_age:
            return item[0]
        return None

    # ---------- MT5 ----------
    def _mt5_connect(self) -> bool:
        if self._mt5 is not None:
            return True
        if self._mt5_failed or time.time() < self._mt5_retry_at:
            return False
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            self._mt5_failed = True
            self.mt5_state = "پکیج MetaTrader5 نصب نیست (برای Python 3.15 منتشر نشده)"
            self.log.info("پکیج MetaTrader5 نصب نیست؛ منبع mt5 غیرفعال شد")
            return False
        kwargs = {"path": self.mt5_path} if self.mt5_path else {}
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            self._mt5_retry_at = time.time() + 60
            self.mt5_state = f"اتصال به ترمینال ناموفق: {err}"
            self.log.warning("MetaTrader 5 وصل نشد (%s)؛ یک دقیقه‌ی دیگر دوباره تلاش می‌شود", err)
            return False
        info = mt5.terminal_info()
        acc = mt5.account_info()
        self._mt5 = mt5
        self.mt5_state = f"وصل: {getattr(info, 'company', '?')} / {getattr(acc, 'server', '?')}"
        self.log.info("اتصال به MetaTrader 5 برقرار شد: %s", self.mt5_state)
        return True

    def resolve_mt5_symbol(self, symbol: str) -> str | None:
        """نام نماد در بروکر (XAUUSD / GOLD / XAUUSDm ...) را پیدا و ذخیره می‌کند."""
        if not self._mt5_connect():
            return None
        if symbol in self._resolved:
            return self._resolved[symbol]
        names = broker_candidates(symbol, self.broker_symbols.get(symbol))
        available = None
        for name in names:
            if self._mt5.symbol_info(name) is not None:
                available = name
                break
        if available is None:
            # جست‌وجوی کلی در نمادهای بروکر (مثلاً «XAUUSD_x»)
            try:
                for s in self._mt5.symbols_get() or []:
                    if normalize_symbol(s.name) == symbol:
                        available = s.name
                        break
            except Exception:  # noqa: BLE001
                pass
        if available:
            self._mt5.symbol_select(available, True)
            self._resolved[symbol] = available
            self.log.info("نماد %s در بروکر MT5 = %s", symbol, available)
        else:
            self.log.warning("نماد %s در بروکر MT5 پیدا نشد؛ market.broker_symbols را تنظیم کنید", symbol)
            self._resolved[symbol] = None
        return available

    def _mt5_price(self, symbol: str) -> float | None:
        try:
            name = self.resolve_mt5_symbol(symbol)
            if not name:
                return None
            tick = self._mt5.symbol_info_tick(name)
            if not tick or tick.bid <= 0:
                return None
            age = time.time() - float(getattr(tick, "time", 0) or 0)
            # تیک کهنه (اتصال ترمینال قطع است) در ساعات باز بازار → مستقل حساب نشود
            if self.market_open(symbol) and age > self.mt5_max_tick_age + 3 * 3600 * 4:
                self.mt5_state = f"تیک {name} کهنه است ({age / 60:.0f} دقیقه)؛ ترمینال وصل است؟"
                return None
            return (tick.bid + tick.ask) / 2 if tick.ask > 0 else tick.bid
        except Exception as e:  # noqa: BLE001
            self.log.warning("خطای MetaTrader 5: %s", e)
            self._mt5 = None
            self._mt5_retry_at = time.time() + 30
            self.mt5_state = f"خطا: {e}"
            return None

    def status(self) -> dict[str, Any]:
        return {"sources": self.sources, "mt5": self.mt5_state, "mt5_symbols": dict(self._resolved),
                "http": {"enabled": bool(self.http_cfg.get("url")), "url": self.http_cfg.get("url") or ""}}

    # ---------- HTTP ----------
    def _new_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=6)

    def _get_http_client(self) -> httpx.AsyncClient:
        """کلاینت پایا: هر بار TLS handshake تازه نمی‌شود (چند صد میلی‌ثانیه صرفه‌جویی در هر قیمت)."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = self._new_http_client()
        return self._http_client

    async def aclose(self) -> None:
        """بستن کلاینت HTTP (پایان جلسه/برنامه). بی‌خطر برای فراخوانی مکرر."""
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http_client = None

    async def _http_price(self, symbol: str) -> float | None:
        url_tpl = self.http_cfg.get("url")
        if not url_tpl:
            return None
        sym = (self.http_cfg.get("symbol_map") or {}).get(symbol, symbol)
        url = url_tpl.format(symbol=sym, base=symbol[:3], quote=symbol[3:])
        headers = self.http_cfg.get("headers") or {}
        # اتصال keep-alive ممکن است بعد از قطعی VPN کهنه شده باشد؛ یک بار با کلاینت تازه تلاش می‌کنیم
        for attempt in (0, 1):
            try:
                r = await self._get_http_client().get(url, headers=headers)
                r.raise_for_status()
                data: Any = r.json()
                for key in str(self.http_cfg.get("json_path", "price")).split("."):
                    data = data[int(key)] if isinstance(data, list) else data[key]
                return float(data)
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    await self.aclose()
                    continue
                self.log.debug("قیمت HTTP برای %s ناموفق: %s", symbol, e)
                return None
        return None

    # ---------- public ----------
    async def get_price(self, symbol: str | None) -> tuple[float | None, str, bool]:
        """(قیمت، منبع، مستقل‌بودن)."""
        if not symbol:
            return None, "none", False
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[1] < self.cache_sec:
            return cached[0], cached[2], cached[2] != "chart"
        for src in self.sources:
            price = None
            if src == "mt5":
                price = await asyncio.to_thread(self._mt5_price, symbol)
            elif src == "http":
                price = await self._http_price(symbol)
            elif src == "chart":
                price = self.chart_price(symbol)
            if price:
                self._cache[symbol] = (price, time.time(), src)
                return price, src, src != "chart"
        return None, "none", False

    @staticmethod
    def market_open(symbol: str | None, now: datetime | None = None) -> bool:
        """بازار فارکس/فلزات/شاخص‌ها: جمعه 21:00 تا یکشنبه 21:00 به وقت UTC بسته. کریپتو همیشه باز."""
        if symbol and get_spec(symbol).kind == "crypto":
            return True
        now = now or datetime.now(timezone.utc)
        wd, hour = now.weekday(), now.hour
        if wd == 5:
            return False
        if wd == 4 and hour >= 21:
            return False
        if wd == 6 and hour < 21:
            return False
        return True
