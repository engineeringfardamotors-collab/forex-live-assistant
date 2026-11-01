"""ارسال به تلگرام با صف پایدار روی دیسک.

- هر پیام اول در outbox.json ذخیره می‌شود، بعد ارسال؛ اگر VPN/اینترنت قطع باشد،
  پس از وصل شدن ارسال می‌شود و اگر دیر رسید، زمان اصلی در آن درج می‌شود.
- پشتیبانی از Reply به پیام اصلی سیگنال و ویرایش پیام (اصلاح سیگنال).
- کانال ادمین جداگانه برای خطاها، سیگنال‌های ردشده و دکمه‌ی تأیید/رد.
- پروکسی صریح (socks5/http) چون Python همیشه از پروکسی سیستم استفاده نمی‌کند.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.utils import atomic_write_text, get_logger


def _proxy_url(cfg: dict[str, Any]) -> str | None:
    p = (cfg.get("network") or {}).get("proxy") or ""
    return p.strip() or None


class TelegramPublisher:
    def __init__(self, cfg: dict[str, Any], state_dir: Path | None = None):
        tg = cfg["telegram"]
        self.token = tg.get("bot_token", "")
        self.channel = tg.get("signal_channel", "")
        self.admin_chat = str(tg.get("admin_chat", "") or "")
        pub = cfg.get("publisher", {})
        self.delay_note_sec = float(pub.get("delay_note_sec", 60))
        self.drop_after_sec = float(pub.get("drop_after_sec", 3600))
        self.retry_sec = float(pub.get("retry_sec", 5))
        self.proxy = _proxy_url(cfg)
        self.log = get_logger("publisher", cfg["logging"]["level"], cfg["logging"]["dir"])
        state_dir = state_dir or Path("session_data")
        state_dir.mkdir(parents=True, exist_ok=True)
        self.queue_file = state_dir / "outbox.json"
        self.sent_file = state_dir / "sent_map.json"
        self._lock = threading.Lock()
        self._queue: list[dict[str, Any]] = self._load(self.queue_file, [])
        self._sent: dict[str, dict[str, Any]] = self._load(self.sent_file, {})
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._cb_task: asyncio.Task | None = None
        self.online: bool | None = None
        self.last_error = ""
        self.on_callback: Callable[[str, str], Awaitable[str | None]] | None = None
        self.on_sent: Callable[[str, float], None] | None = None
        # شناسه‌های محلی محافظت‌شده از هرس (پیام معامله‌های زنده) — TradeBook این را سیم‌کشی می‌کند
        self.protect_ids: Callable[[], set[str]] | None = None
        self.dry_run = bool(pub.get("dry_run", False))
        if self.dry_run:
            self.log.warning("حالت آزمایشی (publisher.dry_run): هیچ پیامی به تلگرام فرستاده نمی‌شود")

    # ---------- persistence ----------
    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except Exception:  # noqa: BLE001
            return default

    def _save(self) -> None:
        with self._lock:
            # نگاشت sent کهنه (۲۴ ساعت) هرس می‌شود؛ اما نگاشت پیام معامله‌های زنده (برای Reply
            # مدیریتی و ویرایش/اصلاحیه) هرگز حذف نمی‌شود — صرف‌نظر از عمرش
            if len(self._sent) > 400:
                cutoff = time.time() - 86400
                protected = set(self.protect_ids()) if self.protect_ids else set()
                pruned = {k: v for k, v in self._sent.items()
                          if float((v or {}).get("sent_at") or 0) >= cutoff or k in protected}
                self._sent = pruned or dict(list(self._sent.items())[-200:])
            try:
                # نوشتن اتمیک با تلاش مجدد — قفل موقت آنتی‌ویروس/ایندکسر برنامه را نمی‌اندازد
                atomic_write_text(self.queue_file, json.dumps(self._queue, ensure_ascii=False, indent=1))
                atomic_write_text(self.sent_file, json.dumps(self._sent, ensure_ascii=False, indent=1))
            except Exception as e:  # noqa: BLE001
                self.log.warning("ذخیره‌ی صف ارسال روی دیسک ناموفق بود (دوباره تلاش می‌شود): %s", e)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.channel and "..." not in self.token)

    # ---------- enqueue API ----------
    def enqueue(self, text: str, *, target: str = "channel", reply_to: str | None = None,
                edit_of: str | None = None, buttons: list[list[dict[str, str]]] | None = None,
                created_at: float | None = None) -> str:
        """target: channel | admin. reply_to/edit_of: شناسه‌ی محلی پیام قبلی. خروجی: شناسه‌ی محلی."""
        if target == "admin" and not self.admin_chat:
            self.log.info("[ادمین] %s", text.replace("\n", " | ")[:300])
            return ""
        item = {
            "id": uuid.uuid4().hex[:12], "target": target, "text": text, "reply_to": reply_to,
            "edit_of": edit_of, "buttons": buttons, "created_at": created_at or time.time(), "attempts": 0,
        }
        with self._lock:
            self._queue.append(item)
        self._save()
        self._wake.set()
        return item["id"]

    async def publish(self, text: str) -> bool:
        """سازگاری با کد قدیمی."""
        if self._task is None:
            self.start()
        self.enqueue(text)
        return True

    def notify_admin(self, text: str) -> None:
        self.enqueue(f"🛠 {text}", target="admin")

    def message_id(self, local_id: str | None) -> int | None:
        if not local_id:
            return None
        return (self._sent.get(local_id) or {}).get("message_id")

    def pending_count(self) -> int:
        return len(self._queue)

    # ---------- Bot API ----------
    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, proxy=self.proxy) if self.proxy else httpx.AsyncClient(timeout=timeout)

    async def _api(self, method: str, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        async with self._client(timeout) as c:
            r = await c.post(url, json=payload)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 429:
            retry = (data.get("parameters") or {}).get("retry_after", 5)
            raise RuntimeError(f"rate-limit:{retry}")
        if not data.get("ok"):
            raise ValueError(f"Telegram {r.status_code}: {str(data)[:300] or r.text[:300]}")
        return data["result"]

    def _chat(self, target: str) -> str:
        return self.admin_chat if target == "admin" else self.channel

    async def _deliver(self, item: dict[str, Any]) -> None:
        text = item["text"]
        age = time.time() - item["created_at"]
        if age > self.delay_note_sec and not item.get("edit_of"):
            t0 = datetime.fromtimestamp(item["created_at"]).strftime("%H:%M:%S")
            text = f"⏱ با تأخیر {int(age // 60)} دقیقه و {int(age % 60)} ثانیه (زمان اصلی {t0})\n" + text
        chat = self._chat(item["target"])
        if item.get("edit_of"):
            mid = self.message_id(item["edit_of"])
            if mid is None:
                if item["edit_of"] in {q["id"] for q in self._queue}:
                    raise RuntimeError("waiting-original")
                raise ValueError("پیام اصلی برای ویرایش پیدا نشد")
            await self._api("editMessageText", {"chat_id": chat, "message_id": mid, "text": text})
            self._sent[item["id"]] = {"message_id": mid, "chat": chat, "sent_at": time.time()}
            return
        payload: dict[str, Any] = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if item.get("reply_to"):
            mid = self.message_id(item["reply_to"])
            if mid is None and item["reply_to"] in {q["id"] for q in self._queue}:
                raise RuntimeError("waiting-original")
            if mid:
                payload["reply_parameters"] = {"message_id": mid, "allow_sending_without_reply": True}
        if item.get("buttons"):
            payload["reply_markup"] = {"inline_keyboard": item["buttons"]}
        res = await self._api("sendMessage", payload)
        self._sent[item["id"]] = {"message_id": res["message_id"], "chat": chat, "sent_at": time.time()}

    async def flush_once(self) -> None:
        if not self.configured or not self._queue:
            return
        changed = False   # فقط وقتی صف/نگاشت عوض شد روی دیسک بنویس (حلقه‌ی هر ۵ ثانیه بی‌دلیل نمی‌نویسد)
        for item in list(self._queue):
            age = time.time() - item["created_at"]
            if age > self.drop_after_sec and item["target"] == "channel" and not item.get("edit_of"):
                self.log.warning("پیام بیش از %d دقیقه در صف ماند و کهنه شد؛ فقط به ادمین گزارش می‌شود",
                                 self.drop_after_sec // 60)
                self._remove(item)
                changed = True
                self.enqueue("پیام کهنه ارسال نشد:\n" + item["text"][:800], target="admin")
                continue
            try:
                if self.dry_run:
                    self._sent[item["id"]] = {"message_id": None, "chat": "dry-run", "sent_at": time.time()}
                    self.log.info("[dry-run → %s] %s", item["target"], item["text"].replace("\n", " | ")[:200])
                else:
                    await self._deliver(item)
                self._remove(item)
                changed = True
                self._notify_sent(item["id"])
                if self.online is not True:
                    self.log.info("اتصال به تلگرام برقرار است")
                self.online = True
                self.log.info("پیام %s به %s ارسال شد", item["id"], item["target"])
            except RuntimeError as e:
                msg = str(e)
                if msg.startswith("rate-limit:"):
                    await asyncio.sleep(float(msg.split(":")[1]))
                break  # حفظ ترتیب
            except ValueError as e:
                self.log.error("پیام %s رد شد: %s", item["id"], e)
                item["attempts"] += 1
                if item["attempts"] >= 3:
                    self._remove(item)
                    changed = True
                break
            except Exception as e:  # noqa: BLE001 شبکه
                if self.online is not False:
                    self.log.warning("اتصال به تلگرام برقرار نیست (%s)؛ پیام‌ها در صف می‌مانند",
                                     type(e).__name__)
                self.online = False
                self.last_error = f"{type(e).__name__}: {e}"
                break
        if changed:
            self._save()

    def _notify_sent(self, local_id: str) -> None:
        if self.on_sent:
            try:
                self.on_sent(local_id, time.time())
            except Exception as e:  # noqa: BLE001
                self.log.debug("on_sent: %s", e)

    def _remove(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._queue = [q for q in self._queue if q["id"] != item["id"]]

    async def _run(self) -> None:
        while True:
            try:
                await self.flush_once()
            except Exception as e:  # noqa: BLE001
                self.log.warning("خطای صف ارسال: %s", e)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.retry_sec)
            except asyncio.TimeoutError:
                pass

    # ---------- approval buttons ----------
    async def _poll_callbacks(self) -> None:
        offset = 0
        while True:
            if not self.configured or not self.admin_chat or self.on_callback is None:
                await asyncio.sleep(10)
                continue
            try:
                updates = await self._api("getUpdates", {"offset": offset, "timeout": 25,
                                                         "allowed_updates": ["callback_query"]}, timeout=40)
                for u in updates:
                    offset = u["update_id"] + 1
                    cq = u.get("callback_query")
                    if not cq:
                        continue
                    data = cq.get("data", "")
                    action, _, ref = data.partition(":")
                    answer = await self.on_callback(action, ref)
                    await self._api("answerCallbackQuery", {"callback_query_id": cq["id"],
                                                            "text": answer or "انجام شد"})
                    msg = cq.get("message") or {}
                    if msg.get("message_id"):
                        await self._api("editMessageReplyMarkup", {
                            "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                            "reply_markup": {"inline_keyboard": [[{"text": answer or "✔", "callback_data": "noop:"}]]},
                        })
            except Exception as e:  # noqa: BLE001
                self.log.debug("getUpdates: %s", e)
                await asyncio.sleep(5)

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = loop.create_task(self._run())
        if self._cb_task is None:
            self._cb_task = loop.create_task(self._poll_callbacks())
        if self._queue:
            self.log.info("%d پیام از اجرای قبلی در صف است", len(self._queue))

    def stop(self) -> None:
        for t in (self._task, self._cb_task):
            if t:
                t.cancel()
        self._task = self._cb_task = None


class MemoryPublisher(TelegramPublisher):
    """ناشر آزمایشی/Replay: پیام‌ها را فقط جمع می‌کند."""

    def __init__(self, cfg: dict[str, Any]):
        self.log = get_logger("publisher", cfg["logging"]["level"], cfg["logging"]["dir"])
        self.admin_chat = "memory"
        self.sent: list[dict[str, Any]] = []
        self._queue = []
        self._sent = {}
        self.online = True
        self.on_callback = None
        self.on_sent = None
        self.dry_run = True
        self._unflushed: list[str] = []

    def enqueue(self, text: str, *, target: str = "channel", reply_to: str | None = None,
                edit_of: str | None = None, buttons: Any = None, created_at: float | None = None) -> str:
        lid = uuid.uuid4().hex[:12]
        self.sent.append({"id": lid, "target": target, "text": text, "reply_to": reply_to,
                          "edit_of": edit_of, "buttons": buttons, "created_at": created_at or time.time()})
        self._sent[lid] = {"message_id": len(self.sent)}
        self._unflushed.append(lid)
        return lid

    async def flush_once(self) -> None:
        """«ارسال» شبیه‌سازی‌شده: فقط رویداد on_sent را برای سنجش تأخیر صدا می‌زند."""
        ids, self._unflushed = self._unflushed, []
        for lid in ids:
            self._notify_sent(lid)

    def start(self) -> None:  # noqa: D401
        return

    def stop(self) -> None:
        return
