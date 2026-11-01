"""ورود خودکار به جلسات آنلاین با Playwright (مرورگر Chromium قابل‌مشاهده).

چون ضبط صفحه با mss از مانیتور انجام می‌شود، مرورگر باید قابل‌دیدن باشد
(headless=False). برای هر پلتفرم، منطق کلیک روی «Join» جداگانه است.
"""
from __future__ import annotations

from typing import Any

from app.utils import get_logger

ZOOM_JOIN_TEXTS = ["Join from Your Browser", "join from your browser"]
COMMON_JOIN_SELECTORS = [
    'button:has-text("Join now")',
    'button:has-text("Join")',
    '[aria-label="Join now"]',
    "#joinBtn",
    'button:has-text("ورود")',
    'button:has-text("پیوستن")',
]


def detect_platform(url: str) -> str:
    u = url.lower()
    if "zoom.us" in u or "zoom.com" in u:
        return "zoom"
    if "meet.google.com" in u:
        return "gmeet"
    if "teams.microsoft.com" in u:
        return "teams"
    if "webex.com" in u:
        return "webex"
    if "bigbluebutton" in u:
        return "bbb"
    return "generic"


class MeetingJoiner:
    def __init__(self, cfg: dict[str, Any]):
        m = cfg["meeting"]
        self.display_name: str = m.get("display_name", "Assistant")
        self.default_password: str = m.get("zoom_password", "") or ""
        self.headless: bool = bool(m.get("headless", False))
        self.timeout_ms: int = int(m.get("join_timeout_sec", 45)) * 1000
        self.manual_wait_sec: int = int(m.get("manual_wait_sec", 600))
        self.user_data_dir: str = m.get("user_data_dir", "") or ""
        self.channel: str = m.get("browser_channel", "") or ""
        self.log = get_logger("browser", cfg["logging"]["level"], cfg["logging"]["dir"])
        self._pw = None
        self.browser = None
        self._ctx = None
        self.page = None

    async def join(self, url: str, password: str | None = None) -> bool:
        """باز کردن جلسه و تلاش برای کلیک روی Join. بهترین‌تلاش — خطا فاجعه نیست."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.log.warning("playwright نصب نیست؛ جلسه فقط با مرورگر پیش‌فرض باز می‌شود")
            import webbrowser

            webbrowser.open(url)
            return False

        pw = password or self.default_password
        platform = detect_platform(url)
        self.log.info("ورود خودکار به جلسه %s (پلتفرم: %s)", url, platform)
        try:
            self._pw = await async_playwright().start()
            args = [
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--start-maximized",
            ]
            # ترجیح: کروم واقعی با پروفایل ماندگار (ورود Google در کروم باندل‌شده معمولاً مسدود می‌شود)
            launched = False
            if self.user_data_dir:
                from pathlib import Path as _Path

                _Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
                for channel in ([self.channel] if self.channel else ["chrome", "msedge", ""]):
                    try:
                        kwargs: dict[str, Any] = {
                            "headless": self.headless,
                            "args": args,
                            "locale": "en-US",
                            "viewport": {"width": 1280, "height": 800},
                        }
                        if channel:
                            kwargs["channel"] = channel
                        self._ctx = await self._pw.chromium.launch_persistent_context(
                            self.user_data_dir, **kwargs
                        )
                        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                        self.log.info("مرورگر با پروفایل ماندگار باز شد (channel=%s)", channel or "chromium")
                        launched = True
                        break
                    except Exception as e:  # noqa: BLE001
                        self.log.debug("راه‌اندازی با channel=%s ناموفق: %s", channel or "chromium", e)

            if not launched:
                self.browser = await self._pw.chromium.launch(headless=self.headless, args=args)
                self._ctx = await self.browser.new_context(
                    locale="en-US", viewport={"width": 1280, "height": 800}
                )
                self.page = await self._ctx.new_page()

            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

            handler = getattr(self, f"_join_{platform}", self._join_generic)
            ok = await handler(pw)
            if ok:
                self.log.info("ورود به جلسه با موفقیت انجام شد ✔")
            else:
                self.log.warning("ورود خودکار کامل نشد؛ ممکن است نیاز به کلیک دستی باشد")
            return ok
        except Exception as e:  # noqa: BLE001
            self.log.error("خطا در ورود خودکار: %s", e)
            return False

    # ---------- helpers ----------
    async def _try_click(self, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                await el.wait_for(state="visible", timeout=4000)
                await el.click()
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _try_fill(self, selectors: list[str], value: str) -> bool:
        if not value:
            return False
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                await el.wait_for(state="visible", timeout=3000)
                await el.fill(value)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # ---------- platforms ----------
    async def _join_zoom(self, password: str) -> bool:
        # صفحه‌ی لانچر: «Join from Your Browser»
        await self._try_click(
            [f'a:has-text("{t}")' for t in ZOOM_JOIN_TEXTS] + ['a[href*="/wc/"]']
        )
        await self.page.wait_for_load_state("domcontentloaded")
        # رمز جلسه (اگر صفحه‌ی پسورد آمد)
        if not await self._try_fill(
            ['input[name="password"]', "#passwordinput", 'input[type="password"]'], password
        ):
            await self.page.wait_for_timeout(2000)
            await self._try_fill(
                ['input[name="password"]', "#passwordinput", 'input[type="password"]'], password
            )
        # نام نمایشی
        await self._try_fill(
            ['input[name="inputname"]', "#inputname", 'input[placeholder*="name" i]'],
            self.display_name,
        )
        # دکمه‌ی Join
        return await self._try_click(
            ['#joinBtn', 'button:has-text("Join")', 'button:has-text("ورود")']
        )

    async def _join_gmeet(self, password: str) -> bool:
        # ممکن است صفحه‌ی مقدماتی «Ask to join»/«Join now» داشته باشد
        for _ in range(3):
            if await self._try_click(
                ['button:has-text("Join now")', '[aria-label="Join now"]', 'button:has-text("Ask to join")']
            ):
                return True
            await self.page.wait_for_timeout(3000)
        return False

    async def wait_for_manual_join(self, timeout_sec: int | None = None) -> bool:
        """پس از ورود دستی کاربر (مثلاً لاگین Google) ورود را تشخیص می‌دهد."""
        import time as _time

        timeout = timeout_sec if timeout_sec is not None else int(self.manual_wait_sec)
        deadline = _time.time() + timeout
        in_call_selectors = [
            'button[aria-label*="Leave call"]',
            'button[aria-label*="Turn off microphone"]',
            'button[aria-label*="ترک"]',
            'button[aria-label*="خروج"]',
            'button:has-text("Leave call")',
            'button:has-text("ترک تماس")',
        ]
        while _time.time() < deadline:
            try:
                for sel in in_call_selectors:
                    if await self.page.locator(sel).count() > 0:
                        self.log.info("ورود به جلسه تشخیص داده شد ✔")
                        return True
                # وجود ویدیوی جلسه نشانه‌ی ورود موفق است
                try:
                    await self.page.wait_for_selector("video", timeout=3000)
                    self.log.info("ورود به جلسه از طریق عناصر ویدیو تشخیص داده شد ✔")
                    return True
                except Exception:  # noqa: BLE001
                    pass
                # اگر کاربر تازه لاگین کرده باشد، دوباره تلاش برای کلیک Join
                if await self._try_click(
                    ['button:has-text("Join now")', '[aria-label="Join now"]', 'button:has-text("Ask to join")']
                ):
                    self.log.info("دکمه‌ی ورود پس از لاگین کلیک شد")
                    return True
            except Exception:  # noqa: BLE001
                pass
            await self.page.wait_for_timeout(5000)
        self.log.warning("در بازه‌ی انتظار، ورود به جلسه تشخیص داده نشد")
        return False

    async def _join_teams(self, password: str) -> bool:
        return await self._try_click(
            ['button:has-text("Join now")', '[aria-label="Join now"]', 'button[role="button"]:has-text("Join")']
        )

    async def _join_webex(self, password: str) -> bool:
        await self._try_fill(['input[placeholder*="name" i]', "#guestDisplayname"], self.display_name)
        return await self._try_click(['button:has-text("Join meeting")', 'button:has-text("Join")'])

    async def _join_bbb(self, password: str) -> bool:
        if not await self._try_fill(
            ['input[placeholder*="name" i]', 'input[name="name"]'], self.display_name
        ):
            await self._try_click(['button:has-text("Enter")', 'a:has-text("Enter")'])
        if await self._try_fill(['input[name="password"]', 'input[type="password"]'], password):
            await self._try_click(['button:has-text("Join")', 'button:has-text("ورود")'])
        return await self._try_click(
            ['button:has-text("Join")', 'button:has-text("Start")', 'a:has-text("Join")']
        )

    async def _join_generic(self, password: str) -> bool:
        # پلتفرم‌های فارسی و ناشناخته: بهترین‌تلاش با انتخابگرهای رایج
        await self._try_fill(['input[name="name"]', 'input[placeholder*="name" i]'], self.display_name)
        await self._try_fill(['input[type="password"]'], password)
        return await self._try_click(COMMON_JOIN_SELECTORS)

    MEETING_ENDED_TEXTS = [
        "You left the meeting", "You've left the meeting", "The meeting has ended", "This meeting has ended",
        "Meeting ended", "The call has ended", "You have been removed", "Returning to home screen",
        "Rejoin", "This meeting has been ended by host", "شما از جلسه خارج شدید", "جلسه به پایان رسید",
        "جلسه تمام شد", "تماس پایان یافت", "پیوستن مجدد", "میزبان جلسه را پایان داد",
    ]

    async def meeting_state(self) -> str:
        """unknown | in_call | ended | closed — وضعیت جلسه از روی صفحه‌ی مرورگر."""
        if self._ctx is None and self.browser is None:
            return "unknown"
        try:
            if self.page is None or self.page.is_closed():
                pages = self._ctx.pages if self._ctx else []
                if not pages:
                    return "closed"
                self.page = pages[-1]
            body = await self.page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)")
            low = body.lower()
            if any(t.lower() in low for t in self.MEETING_ENDED_TEXTS):
                return "ended"
            url = self.page.url.lower()
            if "meet.google.com" in url and url.rstrip("/").endswith("meet.google.com"):
                return "ended"
            if "zoom" in url and ("/postattendee" in url or "/wc/leave" in url):
                return "ended"
            return "in_call"
        except Exception:  # noqa: BLE001
            return "unknown"

    async def close(self) -> None:
        for closer in (self._ctx, self.browser, self._pw):
            try:
                if closer:
                    await closer.close()
            except Exception:  # noqa: BLE001
                pass
        self.browser = None
        self._ctx = None
        self.page = None
        self._pw = None
