"""دریافت لینک جلسه از گروه تلگرام و بازکردن آن در مرورگر پیش‌فرض.

از حساب کاربری Telethon برای گروه‌های خصوصی استفاده می‌شود؛ ربات به‌تنهایی
معمولاً نمی‌تواند همه‌ی گروه‌ها را بخواند.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import TelegramClient, events

from app.utils import get_logger

URL_RE = re.compile(r"https?://[^\s<>\]\[()]+", re.IGNORECASE)
MEETING_HOSTS = ("zoom.us", "zoom.com", "meet.google.com", "teams.microsoft.com", "webex.com", "bigbluebutton")


def telethon_proxy(cfg: dict[str, Any]) -> tuple | None:
    """network.proxy مثل socks5://127.0.0.1:10808 یا http://127.0.0.1:12334 → قالب Telethon."""
    from urllib.parse import urlparse

    raw = ((cfg.get("network") or {}).get("proxy") or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    scheme = {"socks5h": "socks5", "https": "http"}.get(u.scheme, u.scheme)
    if u.username:
        return (scheme, u.hostname, u.port, True, u.username, u.password)
    return (scheme, u.hostname, u.port)


class TelegramLinkListener:
    def __init__(self, cfg: dict[str, Any], on_link: Callable[[str], Awaitable[None]]):
        tg = cfg["telegram"]
        self.groups = tg.get("watch_groups", [])
        self.on_link = on_link
        self.log = get_logger("telegram", cfg["logging"]["level"], cfg["logging"]["dir"])
        self.client = TelegramClient(tg.get("session_name", "assistant_listener"), tg["api_id"], tg["api_hash"],
                                     proxy=telethon_proxy(cfg), connection_retries=None, retry_delay=5,
                                     auto_reconnect=True)

    async def start(self) -> None:
        if not self.groups:
            raise ValueError("telegram.watch_groups تنظیم نشده است")
        await self.client.start()

        # کش کردن دیالوگ‌ها تا شناسه‌های عددی قابل تبدیل به entity باشند
        try:
            await self.client.get_dialogs()
        except Exception as e:  # noqa: BLE001
            self.log.warning("دریافت فهرست دیالوگ‌ها ناموفق: %s", e)

        resolved: list[Any] = []
        for g in self.groups:
            key: Any = int(g) if str(g).lstrip("-").isdigit() else g
            try:
                resolved.append(await self.client.get_entity(key))
            except Exception as e:  # noqa: BLE001
                self.log.warning("گروه %s پیدا نشد: %s", g, e)

        if not resolved:
            raise ValueError("هیچ‌کدام از گروه‌های watch_groups قابل دسترسی نبود")

        self.client.add_event_handler(self._on_message, events.NewMessage(chats=resolved))
        self.log.info("شنود لینک‌های جلسه در %s فعال شد", [g for g in self.groups])
        await self.client.run_until_disconnected()

    async def _on_message(self, event: Any) -> None:
        text = event.raw_text or ""
        for url in URL_RE.findall(text):
            if any(host in url.lower() for host in MEETING_HOSTS) or "join" in url.lower():
                self.log.info("لینک جلسه شناسایی شد: %s", url)
                await self.on_link(url)


async def run_listener(cfg: dict[str, Any], on_link: Callable[[str], Awaitable[None]]) -> None:
    await TelegramLinkListener(cfg, on_link).start()


LOGIN_STATE = "login_state.json"


def _delivery_name(sent: Any) -> str:
    kind = type(getattr(sent, "type", None)).__name__
    return {
        "SentCodeTypeApp": "پیام درون‌برنامه‌ای تلگرام (چت رسمی Telegram)",
        "SentCodeTypeSms": "پیامک (SMS)",
        "SentCodeTypeCall": "تماس صوتی",
        "SentCodeTypeFlashCall": "فلش‌کال",
        "SentCodeTypeFragmentSms": "پیامک Fragment",
    }.get(kind, kind)


async def send_code_step(cfg: dict[str, Any], force_sms: bool = False) -> None:
    """مرحله ۱: درخواست کد و ذخیره‌ی وضعیت برای مرحله‌ی بعد."""
    import json
    from pathlib import Path

    tg = cfg["telegram"]
    client = TelegramClient(tg.get("session_name", "assistant_listener"), tg["api_id"], tg["api_hash"])
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"✅ از قبل وارد شده‌اید: {me.first_name or ''} (@{me.username or me.id})")
            return
        phone = tg.get("phone") or input("شماره تلفن: ").strip()
        sent = await client.send_code_request(phone, force_sms=force_sms)
        Path(LOGIN_STATE).write_text(
            json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"📨 روش ارسال کد: {_delivery_name(sent)}")
        nxt = _delivery_name(type(getattr(sent, "next_type", None))()) if getattr(sent, "next_type", None) else "—"
        print(f"   روش جایگزین در دسترس: {nxt}")
        if getattr(sent, "timeout", None):
            print(f"   تا {sent.timeout} ثانیه دیگر می‌توانید درخواست تازه بدهید")
        print("\nکد را وارد کنید با دستور:")
        print("   python main.py --sign-in <CODE>")
    except Exception as e:  # noqa: BLE001
        print(f"❌ خطا در ارسال کد: {type(e).__name__} — {e}")
    finally:
        await client.disconnect()


async def sign_in_step(cfg: dict[str, Any], code: str) -> None:
    """مرحله ۲: تکمیل ورود با کد دریافتی."""
    import json
    import os
    from pathlib import Path

    from telethon.errors import SessionPasswordNeededError

    if not Path(LOGIN_STATE).exists():
        print("❌ ابتدا «python main.py --send-code» را اجرا کنید.")
        return
    state = json.loads(Path(LOGIN_STATE).read_text(encoding="utf-8"))
    tg = cfg["telegram"]
    client = TelegramClient(tg.get("session_name", "assistant_listener"), tg["api_id"], tg["api_hash"])
    await client.connect()
    try:
        await client.sign_in(state["phone"], code.strip(), phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        pw = os.getenv("TELEGRAM_2FA_PASSWORD") or input("رمز تایید دو مرحله‌ای: ").strip()
        await client.sign_in(password=pw)
    except Exception as e:  # noqa: BLE001
        print(f"❌ ورود ناموفق: {type(e).__name__} — {e}")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"✅ ورود موفق: {me.first_name or ''} (@{me.username or me.id}) — id={me.id}")
    print("فایل session ذخیره شد؛ از این پس شنود گروه بدون کد کار می‌کند.")
    Path(LOGIN_STATE).unlink(missing_ok=True)
    await client.disconnect()


async def list_chats(cfg: dict[str, Any]) -> None:
    """نمایش گروه‌ها/کانال‌های حساب برای انتخاب watch_groups."""
    tg = cfg["telegram"]
    client = TelegramClient(tg.get("session_name", "assistant_listener"), tg["api_id"], tg["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ ابتدا با «python main.py --login» وارد شوید.")
        await client.disconnect()
        return
    print(f"{'id':>16}  {'نوع':<10}  عنوان")
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            kind = "channel" if d.is_channel and not d.is_group else "group"
            print(f"{d.id:>16}  {kind:<10}  {d.title}")
    await client.disconnect()


async def find_chats(cfg: dict[str, Any], query: str) -> None:
    """جست‌وجوی گروه/کانال با بخشی از نام یا یوزرنیم."""
    import re

    tg = cfg["telegram"]
    client = TelegramClient(tg.get("session_name", "assistant_listener"), tg["api_id"], tg["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ ابتدا وارد شوید: python main.py --login")
        await client.disconnect()
        return
    pat = re.compile(re.escape(query), re.IGNORECASE)
    found = 0
    print(f"{'id':>16}  {'نوع':<9}  {'یوزرنیم':<22}  عنوان")
    async for d in client.iter_dialogs():
        title = d.title or ""
        uname = getattr(d.entity, "username", None) or ""
        if pat.search(title) or (uname and pat.search(uname)):
            kind = "group" if d.is_group else ("channel" if d.is_channel else "user")
            print(f"{d.id:>16}  {kind:<9}  {('@' + uname) if uname else '—':<22}  {title}")
            found += 1
    if not found:
        print("(چیزی پیدا نشد — شاید هنوز عضو گروه نشده‌اید)")
    await client.disconnect()


async def interactive_login(cfg: dict[str, Any], force_sms: bool = False) -> None:
    """ورود یک‌بارهٔ حساب تلگرام (کد تایید در ترمینال خودتان وارد می‌شود)."""
    from telethon.errors import SessionPasswordNeededError

    tg = cfg["telegram"]
    log = get_logger("telegram-login", cfg["logging"]["level"], cfg["logging"]["dir"])
    if not tg.get("api_id") or not tg.get("api_hash"):
        print("❌ TELEGRAM_API_ID / TELEGRAM_API_HASH تنظیم نشده است")
        return

    client = TelegramClient(tg.get("session_name", "assistant_listener"), tg["api_id"], tg["api_hash"])
    print(f"اتصال با api_id={tg['api_id']} ...")
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"✅ از قبل وارد شده‌اید: {me.first_name or ''} (@{me.username or me.id})")
            await client.disconnect()
            return

        phone = tg.get("phone") or input("شماره تلفن (مثل +98912...): ").strip()

        def show_method(sent: Any) -> None:
            kind = type(getattr(sent, "type", None)).__name__
            names = {
                "SentCodeTypeApp": "پیام درون‌برنامه‌ای تلگرام (چت رسمی Telegram)",
                "SentCodeTypeSms": "پیامک (SMS)",
                "SentCodeTypeCall": "تماس صوتی",
                "SentCodeTypeFlashCall": "فلش‌کال",
                "SentCodeTypeFragmentSms": "پیامک Fragment",
            }
            print(f"📨 روش ارسال کد: {names.get(kind, kind)}")
            timeout = getattr(sent, "timeout", None)
            if timeout:
                print(f"   (تا {timeout} ثانیه دیگر می‌توانید درخواست تازه بدهید)")

        sent = await client.send_code_request(phone, force_sms=force_sms)
        show_method(sent)
        print("اگر کد نیامد: داخل اپ تلگرام، چت رسمی «Telegram» (تیک آبی) را ببینید.")
        print("برای ارسال دوباره با پیامک، به‌جای کد عبارت «r» را بزنید.\n")

        signed = False
        for attempt in range(1, 4):
            code = input(f"کد تایید (تلاش {attempt}/3، یا r برای پیامک): ").strip()
            if code.lower() == "r":
                try:
                    sent = await client.send_code_request(phone, force_sms=True)
                    show_method(sent)
                except Exception as e:  # noqa: BLE001
                    print(f"⚠️ ارسال پیامک ناموفق: {e}")
                continue
            try:
                await client.sign_in(phone, code)
                signed = True
                break
            except SessionPasswordNeededError:
                pw = input("رمز تایید دو مرحله‌ای: ").strip()
                await client.sign_in(password=pw)
                signed = True
                break
            except Exception as e:  # noqa: BLE001
                print(f"❌ کد پذیرفته نشد: {type(e).__name__} — {e}")

        if not signed:
            print("❌ ورود انجام نشد.")
            await client.disconnect()
            return

        me = await client.get_me()
        print(f"✅ ورود موفق: {me.first_name or ''} (@{me.username or me.id})")
        print("فایل session ذخیره شد؛ از این پس شنود گروه بدون کد کار می‌کند.")
        await client.disconnect()
    except EOFError:
        print("❌ ورود تعاملی نیاز به ترمینال دارد. این دستور را در پنجره PowerShell خودتان اجرا کنید.")
    except Exception as e:  # noqa: BLE001
        log.error("خطا در ورود تلگرام: %s", e)
        print(f"❌ خطا: {type(e).__name__} — {e}")
