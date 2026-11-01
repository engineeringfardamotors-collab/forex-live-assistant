"""ماشین حالت معاملات جلسه: پیش‌نویس، ارسال، اصلاح، مدیریت، لغو، بازبینی.

    DRAFT ──(کامل شد)──► SENT ──(قیمت به ورود رسید)──► ACTIVE
      │                   │                           │
   (انقضا)             (لغو)              ┌───────────┼───────────┐
      ▼                   ▼               ▼           ▼           ▼
   EXPIRED           CANCELLED       BREAKEVEN     TP_HIT      SL_HIT
                                                      │
                                                   CLOSED
    REVIEW ──(تأیید ادمین)──► SENT     REVIEW ──(رد)──► REJECTED

پیام‌های مدیریتی به‌صورت Reply به پیام اصلی سیگنال ارسال می‌شوند.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import clock
from app.publisher import TelegramPublisher
from app.signal_model import Issue, Signal, merge_signals
from app.symbols import fmt_price, get_spec
from app.utils import atomic_write_text, get_logger

OPEN_STATES = {"SENT", "ACTIVE", "BREAKEVEN", "PARTIAL", "TP_HIT"}
ALL_LIVE_STATES = OPEN_STATES | {"REVIEW"}

DISCLAIMER = "ℹ️ استخراج خودکار از جلسه‌ی زنده؛ ممکن است خطا داشته باشد. توصیه‌ی سرمایه‌گذاری نیست."

SIDE_FA = {"BUY": "🟢 خرید", "SELL": "🔴 فروش"}
TYPE_FA = {
    ("BUY", "MARKET"): "Buy (از قیمت فعلی)", ("SELL", "MARKET"): "Sell (از قیمت فعلی)",
    ("BUY", "LIMIT"): "Buy Limit", ("SELL", "LIMIT"): "Sell Limit",
    ("BUY", "STOP"): "Buy Stop", ("SELL", "STOP"): "Sell Stop",
}
MGMT_FA = {
    "BREAKEVEN": "🛡 ریسک‌فری: حد ضرر را به نقطه‌ی ورود منتقل کنید",
    "MOVE_SL": "🔁 جابه‌جایی حد ضرر",
    "PARTIAL_CLOSE": "💵 سیو سود / بستن بخشی از پوزیشن",
    "CLOSE": "⛔️ بستن کامل پوزیشن",
    "TP_HIT": "✅ تارگت خورد",
    "SL_HIT": "❌ حد ضرر خورد",
    "ADD_ENTRY": "➕ افزودن پله‌ی ورود",
}


@dataclass
class Trade:
    id: str
    signal: Signal
    status: str
    created_at: float = field(default_factory=clock.now)
    updated_at: float = field(default_factory=clock.now)
    msg_id: str | None = None          # شناسه‌ی محلی پیام کانال
    review_msg_id: str | None = None   # شناسه‌ی محلی پیام ادمین
    last_msg_id: str | None = None     # آخرین پیام کانال مربوط (ویرایش/Reply) برای سنجش تأخیر
    parent_id: str | None = None
    issues: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def log(self, event: str, **data: Any) -> None:
        self.updated_at = clock.now()
        self.history.append({"t": self.updated_at, "event": event, **data})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "signal": self.signal.to_dict(), "status": self.status, "created_at": self.created_at,
            "updated_at": self.updated_at, "msg_id": self.msg_id, "review_msg_id": self.review_msg_id,
            "last_msg_id": self.last_msg_id, "parent_id": self.parent_id, "issues": self.issues,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trade":
        return cls(
            id=str(d["id"]), signal=Signal.from_dict(d.get("signal") or {}),
            status=str(d.get("status") or "SENT"), created_at=float(d.get("created_at") or 0.0),
            updated_at=float(d.get("updated_at") or 0.0), msg_id=d.get("msg_id"),
            review_msg_id=d.get("review_msg_id"), last_msg_id=d.get("last_msg_id"),
            parent_id=d.get("parent_id"), issues=list(d.get("issues") or []),
            history=list(d.get("history") or []),
        )


class TradeBook:
    def __init__(self, cfg: dict[str, Any], publisher: TelegramPublisher, state_file: Path | None = None):
        self.cfg = cfg
        t = cfg.get("trades", {})
        self.update_window = float(t.get("update_window_min", 30)) * 60
        self.update_entry_pct = float(t.get("update_entry_pct", 0.1))
        self.draft_ttl = float(t.get("draft_ttl_sec", 120))
        self.review_ttl = float(t.get("review_ttl_sec", 300))
        self.spec_overrides = cfg.get("symbols", {}).get("overrides") or {}
        self.pub = publisher
        self.persist = bool(t.get("persist", True))
        self.state_file = state_file
        if not self.persist:
            self.state_file = None
        # سقف فاصله‌ی زمانی آخرین ذخیره برای بازیابی (توقف کنسول/ویندوز/برق = کرش حساب می‌شود)
        self.restore_max_gap = float(t.get("restore_max_gap_min", 120)) * 60
        self._session_ended = False   # پایان تمیز جلسه (کرش = False → شروع بعدی معاملات را بازیابی می‌کند)
        self.log = get_logger("trades", cfg["logging"]["level"], cfg["logging"]["dir"])
        self.trades: dict[str, Trade] = {}
        self.drafts: dict[str, tuple[Signal, float]] = {}
        self._seq = 0
        self.last_trade: Trade | None = None
        self.stats = {"published": 0, "review": 0, "rejected": 0, "updates": 0, "management": 0, "duplicates": 0}

    # ---------- helpers ----------
    def _new_id(self) -> str:
        self._seq += 1
        return f"T{self._seq}"

    def _spec(self, symbol: str):
        return get_spec(symbol, self.spec_overrides)

    def open_trades(self, symbol: str | None = None) -> list[Trade]:
        return [t for t in self.trades.values()
                if t.status in OPEN_STATES and (symbol is None or t.signal.symbol == symbol)]

    def open_trades_text(self) -> str:
        rows = []
        for t in self.trades.values():
            if t.status not in ALL_LIVE_STATES:
                continue
            s, spec = t.signal, self._spec(t.signal.symbol)
            rows.append(
                f"- id={t.id} {s.symbol} {s.side} {s.order_type or ''} entry={fmt_price(s.entry_ref(), spec)} "
                f"sl={fmt_price(s.sl, spec)} tps={[fmt_price(x, spec) for x in s.tps]} وضعیت={t.status}"
            )
        return "\n".join(rows)

    def protected_msg_ids(self) -> set[str]:
        """شناسه‌های محلی پیام معامله‌های زنده (SENT/ACTIVE/…/REVIEW).

        صف ارسال هنگام هرس نگاشت sent این شناسه‌ها را حذف نمی‌کند؛ چون تا وقتی معامله
        باز است ممکن است Reply مدیریتی یا ویرایش (اصلاحیه) به پیامش نیاز باشد.
        """
        ids: set[str] = set()
        for t in list(self.trades.values()):
            if t.status in ALL_LIVE_STATES:
                for mid in (t.msg_id, t.review_msg_id, t.last_msg_id):
                    if mid:
                        ids.add(mid)
        return ids

    def save(self) -> None:
        if not self.state_file:
            return
        data = {"seq": self._seq,
                "saved_at": time.time(),
                "session_ended": self._session_ended,
                "trades": [t.to_dict() for t in self.trades.values()],
                "drafts": {k: [v[0].to_dict(), v[1]] for k, v in self.drafts.items()},
                "stats": self.stats}
        try:
            # نوشتن اتمیک با تلاش مجدد: کرش وسط ذخیره یا قفل موقت آنتی‌ویروس فایل را خراب نمی‌کند
            atomic_write_text(self.state_file, json.dumps(data, ensure_ascii=False, indent=1))
        except Exception as e:  # noqa: BLE001
            self.log.warning("ذخیره‌ی وضعیت معاملات ناموفق: %s", e)

    def close_session(self) -> None:
        """پایان تمیز جلسه: در شروع بعدی معامله‌های این جلسه بازیابی نمی‌شوند.

        بازیابی بعد از ری‌استارت فقط برای «کرش» است؛ اگر جلسه خودش تمام شده باشد
        (پایان طبیعی، سکوت، سقف زمان، Ctrl+C) جلسه‌ی بعدی جلسه‌ی جدیدی است.
        """
        self._session_ended = True
        self.save()

    def load(self) -> None:
        """بازیابی وضعیت پس از ری‌استارت برنامه.

        اگر اجرای قبلی با «پایان جلسه» بسته شده باشد، هیچ معامله‌ای بازیابی نمی‌شود
        (جلسه‌ی جدید = معاملات جدید). اگر اجرای قبلی کرش کرده باشد، همه‌ی معامله‌های
        زنده بازیابی می‌شوند تا مدیریت/اصلاحیه‌ی آن‌ها ادامه یابد و نگاشت پیامشان
        (protected_msg_ids) از هرس صف ارسال محافظت شود — اما فقط اگر فایل تازه باشد:
        هر توقفی غیر از end_meeting (بستن کنسول، خاموشی ویندوز، قطع برق) هم «کرش» است و
        ویندوز ممکن است فردا صبح باز شود؛ فایل کهنه‌تر از restore_max_gap_min یعنی جلسه
        قطعاً تمام شده و هیچ معامله‌ای بازیابی نمی‌شود (فقط هشدار + حفظ شماره‌ی شناسه‌ها).
        """
        if not self.state_file or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            self.log.warning("بارگذاری وضعیت معاملات ناموفق (%s)؛ از صفر شروع می‌شود", e)
            return
        self._seq = int(data.get("seq") or 0)
        for d in data.get("trades") or []:
            try:
                t = Trade.from_dict(d)
            except Exception as e:  # noqa: BLE001
                self.log.debug("بازیابی یک معامله ناموفق: %s", e)
                continue
            if t.id[1:].isdigit():
                self._seq = max(self._seq, int(t.id[1:]))
        if bool(data.get("session_ended")):
            self._session_ended = False
            self.log.info("اجرای قبلی با پایان جلسه بسته شده؛ معامله‌ای بازیابی نمی‌شود")
            self.stats.update(data.get("stats") or {})
            return
        saved_at = float(data.get("saved_at") or 0)
        gap_min = (time.time() - saved_at) / 60 if saved_at else None
        if gap_min is None or gap_min > self.restore_max_gap / 60:
            open_count = sum(1 for d in data.get("trades") or []
                             if str(d.get("status") or "") in ALL_LIVE_STATES)
            self.log.warning("آخرین ذخیره‌ی معاملات %s است (سقف بازیابی restore_max_gap_min=%d)؛ "
                             "%d معامله‌ی باز بازیابی نشد تا آپدیت امروز روی سیگنال دیروز نرود",
                             f"{gap_min:.0f} دقیقه پیش" if gap_min is not None else "بدون زمان",
                             int(self.restore_max_gap / 60), open_count)
            self.stats.update(data.get("stats") or {})
            return
        now = clock.now()
        restored = 0
        for d in data.get("trades") or []:
            try:
                t = Trade.from_dict(d)
            except Exception:  # noqa: BLE001
                continue
            if t.status in ALL_LIVE_STATES:
                self.trades[t.id] = t
                restored += 1
        for k, v in (data.get("drafts") or {}).items():
            try:
                sig_d, ts = v[0], float(v[1])
            except Exception:  # noqa: BLE001
                continue
            if now - ts <= self.draft_ttl:
                try:
                    self.drafts[k] = (Signal.from_dict(sig_d), ts)
                except Exception:  # noqa: BLE001
                    continue
        self.stats.update(data.get("stats") or {})
        if self.trades:
            self.last_trade = max(self.trades.values(), key=lambda t: t.created_at)
        if restored:
            self.log.info("%d معامله‌ی زنده از اجرای قبل بازیابی شد (ری‌استارت/کرش)", restored)

    # ---------- drafts ----------
    def merge_draft(self, sig: Signal) -> Signal:
        key = f"{sig.symbol}"
        self._expire_drafts()
        prev = self.drafts.get(key)
        if prev and (sig.side is None or prev[0].side is None or prev[0].side == sig.side):
            merged = merge_signals(prev[0], sig)
            self.log.info("پیش‌نویس %s با اطلاعات جدید تکمیل شد", key)
            return merged
        return sig

    def store_draft(self, sig: Signal) -> None:
        self.drafts[sig.symbol] = (sig, clock.now())
        self.log.info("پیش‌نویس ناقص نگه داشته شد: %s %s entry=%s sl=%s", sig.symbol, sig.side,
                      sig.entry_ref(), sig.sl)

    def clear_draft(self, symbol: str) -> None:
        self.drafts.pop(symbol, None)

    def _expire_drafts(self) -> None:
        now = clock.now()
        for k, (s, t) in list(self.drafts.items()):
            if now - t > self.draft_ttl:
                self.drafts.pop(k)
                self.log.info("پیش‌نویس %s منقضی شد (تکمیل نشد)", k)
                self.pub.notify_admin(f"پیش‌نویس ناقص منقضی شد:\n{self.format_signal(s, 'DRAFT', admin=True)}")

    def tick(self, market_prices: dict[str, float] | None = None) -> None:
        """نگهداری دوره‌ای: انقضای پیش‌نویس/بازبینی و فعال‌شدن سفارش معلق با قیمت بازار.

        از حلقه‌ی پایش (هر ~۱۵ ثانیه در جلسه‌ی فعال) صدا زده می‌شود؛ همین ذخیره‌ی دوره‌ای
        `saved_at` را تازه نگه می‌دارد تا ری‌استارت فوری در جلسه‌ی طولانیِ بدون تغییر
        معاملاتی هم معامله‌های باز را بازیابی کند. ذخیره در finally تضمین شده تا خطای
        پردازش درونی، تازه‌ماندن saved_at را متوقف نکند.
        """
        try:
            self._expire_drafts()
            now = clock.now()
            for t in self.trades.values():
                if t.status == "REVIEW" and now - t.created_at > self.review_ttl:
                    t.status = "EXPIRED"
                    t.log("review_expired")
                    self.pub.notify_admin(f"⌛ بازبینی {t.id} بی‌پاسخ ماند و منقضی شد (ارسال نشد).")
                if t.status == "SENT" and market_prices and t.signal.symbol in market_prices:
                    p, s = market_prices[t.signal.symbol], t.signal
                    e = s.entry_ref()
                    if s.order_type == "MARKET" or e is None:
                        t.status = "ACTIVE"
                    elif (s.order_type == "LIMIT" and ((s.side == "BUY" and p <= e) or (s.side == "SELL" and p >= e))) or \
                         (s.order_type == "STOP" and ((s.side == "BUY" and p >= e) or (s.side == "SELL" and p <= e))):
                        t.status = "ACTIVE"
                        t.log("activated", price=p)
                        self.log.info("سفارش %s فعال شد (قیمت %s)", t.id, p)
        finally:
            self.save()

    # ---------- entries ----------
    def find_similar(self, sig: Signal) -> Trade | None:
        """یافتن معامله‌ی مشابه برای تشخیص «تکرار» یا «اصلاح» سیگنال جدیدِ ورود.

        پنجره‌ی ۳۰ دقیقه از **آخرین فعالیت** معامله (updated_at) است، نه ایجاد؛ معامله‌ای که
        تازه مدیریت/اصلاح شده هنوز «همان سیگنال» است. دستورهای مدیریتی محدودیت زمانی ندارند
        (تا وقتی معامله باز است) — این پنجره فقط برای سیگنال ورود جدید است.
        """
        now = clock.now()
        e_new = sig.entry_ref()
        best = None
        for t in self.trades.values():
            if t.status not in ALL_LIVE_STATES or t.signal.symbol != sig.symbol or t.signal.side != sig.side:
                continue
            if now - t.updated_at > self.update_window:
                continue
            e_old = t.signal.entry_ref()
            if e_old is None or e_new is None:
                if sig.order_type == "MARKET" and t.signal.order_type == "MARKET":
                    best = t
                continue
            if abs(e_new - e_old) / e_old * 100 <= self.update_entry_pct:
                best = t
        return best

    def handle_entry(self, sig: Signal, issues: list[Issue], decision: str, market_open: bool = True) -> str:
        """decision از signal_model.route. خروجی: published|updated|duplicate|review|rejected|draft."""
        spec = self._spec(sig.symbol)
        if decision == "draft":
            self.store_draft(sig)
            return "draft"
        self.clear_draft(sig.symbol)

        if decision == "reject":
            self.stats["rejected"] += 1
            reasons = "\n".join(f"• {i}" for i in issues) or f"intent={sig.intent}، اطمینان={sig.confidence:.2f}"
            self.log.info("سیگنال رد شد: %s %s (%s)", sig.symbol, sig.side, reasons.replace("\n", " "))
            if sig.intent not in {"NOT_A_SIGNAL", "BIAS_ONLY"} and sig.side:
                self.pub.notify_admin(f"🚫 رد شد ({sig.intent}):\n{self.format_signal(sig, 'REJECTED', admin=True)}"
                                      f"\nدلایل:\n{reasons}")
            return "rejected"

        existing = self.find_similar(sig)
        self.last_trade = existing
        if existing:
            if existing.signal.core_key(spec) == sig.core_key(spec):
                self.stats["duplicates"] += 1
                self.log.info("سیگنال تکراری %s نادیده گرفته شد", existing.id)
                return "duplicate"
            return self._update(existing, sig, issues, decision)

        trade = Trade(id=self._new_id(), signal=sig, status="REVIEW" if decision == "review" else "SENT",
                      issues=[str(i) for i in issues])
        self.trades[trade.id] = trade
        self.last_trade = trade
        if not market_open:
            sig.notes.append("بازار در این زمان بسته است")
        if decision == "review":
            self._send_review(trade, issues)
            result = "review"
        else:
            self._publish_trade(trade)
            result = "published"
        self.save()
        return result

    def _publish_trade(self, trade: Trade) -> None:
        trade.status = "SENT"
        trade.msg_id = self.pub.enqueue(self.format_signal(trade.signal, trade.id),
                                        created_at=trade.signal.spoken_at or trade.created_at)
        trade.log("published")
        self.stats["published"] += 1
        self.log.info("سیگنال %s منتشر شد: %s %s", trade.id, trade.signal.symbol, trade.signal.side)

    def _send_review(self, trade: Trade, issues: list[Issue]) -> None:
        self.stats["review"] += 1
        reasons = "\n".join(f"• {i}" for i in issues) or f"• اطمینان {trade.signal.confidence:.0%} کمتر از حد ارسال خودکار"
        buttons = [[{"text": "✅ تأیید و ارسال", "callback_data": f"approve:{trade.id}"},
                    {"text": "❌ رد", "callback_data": f"reject:{trade.id}"}]]
        trade.review_msg_id = self.pub.enqueue(
            f"🟡 نیاز به تأیید ({trade.id})\n\n{self.format_signal(trade.signal, trade.id, admin=True)}\n\nدلایل:\n{reasons}",
            target="admin", buttons=buttons)
        if not self.pub.admin_chat or self.pub.admin_chat == "":
            # بدون کانال ادمین: نسخه‌ی برچسب‌دار به کانال اصلی
            if self.cfg.get("validation", {}).get("review_fallback", "label") == "label":
                trade.msg_id = self.pub.enqueue("⚠️ نیاز به تأیید — با احتیاط\n" + self.format_signal(trade.signal, trade.id))
                trade.status = "SENT"
        trade.log("review", reasons=reasons)
        self.log.info("سیگنال %s به بازبینی رفت", trade.id)

    def _update(self, trade: Trade, sig: Signal, issues: list[Issue], decision: str) -> str:
        old = trade.signal
        spec = self._spec(sig.symbol)
        merged = merge_signals(old, sig)
        changes = []
        for f, label in (("entry", "ورود"), ("sl", "حد ضرر")):
            a, b = getattr(old, f), getattr(merged, f)
            if a != b and b is not None:
                changes.append(f"{label}: {fmt_price(a, spec)} ← {fmt_price(b, spec)}")
        if old.tps != merged.tps:
            changes.append("اهداف: " + " | ".join(fmt_price(x, spec) for x in merged.tps))
        trade.signal = merged
        trade.log("updated", changes=changes)
        self.stats["updates"] += 1
        if trade.status == "REVIEW":
            self.pub.notify_admin(f"✏️ {trade.id} (در بازبینی) اصلاح شد:\n" + "\n".join(changes))
        elif decision == "reject":
            self.pub.notify_admin(f"⚠️ اصلاحیه‌ی {trade.id} رد شد: " + "; ".join(str(i) for i in issues))
        elif trade.msg_id:
            trade.last_msg_id = self.pub.enqueue("✏️ اصلاح شد\n" + self.format_signal(merged, trade.id),
                                                 edit_of=trade.msg_id)
            self.pub.enqueue("✏️ اصلاحیه‌ی سیگنال:\n" + "\n".join(changes or ["جزئیات به‌روز شد"]),
                             reply_to=trade.msg_id)
        self.log.info("سیگنال %s اصلاح شد: %s", trade.id, changes)
        self.save()
        return "updated"

    # ---------- management / cancel ----------
    def _resolve_target(self, target_id: str | None, symbol: str | None, statuses: set[str]) -> Trade | None:
        if target_id and target_id in self.trades and self.trades[target_id].status in statuses:
            return self.trades[target_id]
        cands = [t for t in self.trades.values() if t.status in statuses and (symbol is None or t.signal.symbol == symbol)]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            return max(cands, key=lambda t: t.updated_at)
        return None

    def handle_management(self, event: dict[str, Any], symbol: str | None, confidence: float,
                          spec_price: Any = None) -> str:
        m = event.get("management") or {}
        action = str(m.get("action") or "").upper()
        min_conf = float(self.cfg.get("validation", {}).get("min_management_confidence", 0.6))
        trade = self._resolve_target(event.get("target_trade_id"), symbol, OPEN_STATES | {"REVIEW"})
        if not action or action == "NULL":
            return "ignored"
        if trade is None:
            self.pub.notify_admin(f"دستور مدیریتی «{action}» برای {symbol or '?'} آمد ولی معامله‌ی بازی پیدا نشد.\n"
                                  f"🗣 {event.get('evidence_quote', '')}")
            return "no_target"
        if confidence < min_conf:
            self.pub.notify_admin(f"دستور مدیریتی کم‌اطمینان ({confidence:.0%}) برای {trade.id}: {action}\n"
                                  f"🗣 {event.get('evidence_quote', '')}")
            return "low_confidence"
        spec = self._spec(trade.signal.symbol)
        s = trade.signal
        from app.symbols import expand_short_price, to_float

        def p(v: Any) -> float | None:
            f = to_float(v)
            return expand_short_price(f, s.entry_ref(), spec) if f is not None else None

        lines = [MGMT_FA.get(action, action)]
        if action == "BREAKEVEN":
            s.sl = s.entry_ref()
            trade.status = "BREAKEVEN"
            lines.append(f"حد ضرر جدید: {fmt_price(s.sl, spec)}")
        elif action == "MOVE_SL":
            new_sl = p(m.get("new_sl"))
            if new_sl is None:
                return "incomplete"
            ref = s.entry_ref()
            # حد ضرر جدید نباید آن‌سوی تارگت اول باشد
            if s.tps and ((s.side == "BUY" and new_sl >= s.tps[0]) or (s.side == "SELL" and new_sl <= s.tps[0])):
                self.pub.notify_admin(f"جابه‌جایی استاپ {trade.id} به {new_sl} غیرمنطقی است؛ ارسال نشد.")
                return "rejected"
            lines.append(f"حد ضرر: {fmt_price(s.sl, spec)} ← {fmt_price(new_sl, spec)}")
            s.sl = new_sl
            if ref is not None and ((s.side == "BUY" and new_sl >= ref) or (s.side == "SELL" and new_sl <= ref)):
                trade.status = "BREAKEVEN"
        elif action == "PARTIAL_CLOSE":
            pct = to_float(m.get("percent"))
            if pct:
                lines.append(f"میزان: {pct:g}٪ از حجم")
            trade.status = "PARTIAL"
        elif action == "CLOSE":
            trade.status = "CLOSED"
        elif action == "TP_HIT":
            idx = int(to_float(m.get("tp_index")) or 1)
            lines[0] = f"✅ تارگت {idx} خورد"
            trade.status = "TP_HIT" if idx < max(1, len(s.tps)) else "CLOSED"
        elif action == "SL_HIT":
            trade.status = "SL_HIT"
        elif action == "ADD_ENTRY":
            price = p(m.get("price"))
            if price is not None:
                # سهم حجم پله: «نصف حجم» = 0.5 / «۳۰ درصد» = 0.3 (مدل به ۰..۱ می‌دهد، درصد هم می‌پذیریم)
                pct = to_float(m.get("portion"))
                if pct is None:
                    pct = to_float(m.get("percent"))
                if pct and pct > 1:
                    pct = pct / 100
                portion = float(pct or 0.0)
                s.entries.append({"price": price, "portion": portion})
                lines.append(f"ورود دوم: {fmt_price(price, spec)}"
                             + (f" ({portion * 100:.0f}٪ حجم)" if portion else ""))
        elif action == "REENTRY":
            return "reentry"   # به‌صورت سیگنال جدید (ACTUAL_ENTRY) پردازش می‌شود
        else:
            return "ignored"

        trade.log("management", action=action, data=m)
        self.stats["management"] += 1
        quote = event.get("evidence_quote")
        text = f"{s.symbol} · {trade.id}\n" + "\n".join(lines) + (f"\n🗣 «{quote}»" if quote else "")
        if trade.status == "REVIEW" or not trade.msg_id:
            self.pub.notify_admin("(مدیریت معامله‌ی در بازبینی)\n" + text)
        else:
            self.pub.enqueue(text, reply_to=trade.msg_id)
        self.log.info("مدیریت %s: %s", trade.id, action)
        self.save()
        return "managed"

    def handle_cancel(self, event: dict[str, Any], symbol: str | None, confidence: float) -> str:
        trade = self._resolve_target(event.get("target_trade_id"), symbol, {"SENT", "REVIEW", "ACTIVE"})
        if trade is None:
            self.pub.notify_admin(f"دستور لغو آمد ولی سیگنال مرتبطی پیدا نشد: {event.get('evidence_quote', '')}")
            return "no_target"
        if confidence < float(self.cfg.get("validation", {}).get("min_management_confidence", 0.6)):
            self.pub.notify_admin(f"لغو کم‌اطمینان برای {trade.id}: {event.get('evidence_quote', '')}")
            return "low_confidence"
        prev = trade.status
        trade.status = "CANCELLED"
        trade.log("cancelled")
        if prev != "REVIEW" and trade.msg_id:
            label = "❎ سفارش معلق لغو شد" if trade.signal.order_type in {"LIMIT", "STOP"} and prev == "SENT" \
                else "❎ این سیگنال از سوی تحلیلگر لغو شد"
            self.pub.enqueue(f"{label} ({trade.id})", reply_to=trade.msg_id)
        else:
            self.pub.notify_admin(f"{trade.id} قبل از ارسال لغو شد.")
        self.save()
        return "cancelled"

    # ---------- admin callbacks ----------
    async def on_callback(self, action: str, ref: str) -> str | None:
        trade = self.trades.get(ref)
        if not trade:
            return "پیدا نشد"
        if trade.status != "REVIEW":
            return f"وضعیت فعلی: {trade.status}"
        if action == "approve":
            self._publish_trade(trade)
            trade.log("approved")
            self.save()
            return "ارسال شد ✅"
        if action == "reject":
            trade.status = "REJECTED"
            trade.log("rejected_by_admin")
            self.save()
            return "رد شد ❌"
        return None

    # ---------- formatting ----------
    def _render_template(self, s: Signal, trade_id: str) -> str | None:
        """پیام سیگنال با قالب دلخواه `signal.template` (متغیرها: {symbol} {side} {order_type} ...).

        اگر قالب تنظیم نشده یا خالی باشد None برمی‌گردد تا متن پیش‌فرض استفاده شود.
        """
        tmpl = str((self.cfg.get("signal") or {}).get("template") or "").strip()
        if not tmpl:
            return None
        spec = self._spec(s.symbol)
        parts = [f"{fmt_price(e['price'], spec)}" + (f" ({e['portion'] * 100:.0f}٪)" if e.get("portion") else "")
                 for e in s.entries]
        vars_ = {
            "symbol": s.symbol,
            "side": SIDE_FA.get(s.side or "", s.side or "?"),
            "side_raw": s.side or "",
            "order_type": TYPE_FA.get((s.side or "", s.order_type or ""), s.order_type or "?"),
            "entry": fmt_price(s.entry_ref(), spec),
            "sl": fmt_price(s.sl, spec),
            "tps": " | ".join(f"TP{i + 1}: {fmt_price(t, spec)}" for i, t in enumerate(s.tps)),
            "entries": " | ".join(parts),
            "timeframe": s.timeframe or "",
            "confidence": f"{s.confidence:.0%}",
            "evidence": (s.evidence or "")[:250],
            "condition": s.condition or "",
            "id": trade_id,
            "time": clock.hhmmss(s.spoken_at or s.created_at),
        }
        out = tmpl
        for k, v in vars_.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def format_signal(self, s: Signal, trade_id: str, admin: bool = False) -> str:
        spec = self._spec(s.symbol)
        fp = lambda v: fmt_price(v, spec)  # noqa: E731
        head = f"⚡ سیگنال {s.symbol} — {SIDE_FA.get(s.side or '', s.side or '?')}"
        otype = TYPE_FA.get((s.side or "", s.order_type or ""))
        if otype:
            head += f" ({otype})"
        lines = [head]
        if s.intent == "CONDITIONAL":
            lines.insert(0, "⚠️ سیگنال شرطی — فقط در صورت تحقق شرط")
            if s.condition:
                lines.append(f"📌 شرط: {s.condition}")
        if s.entry_low is not None and s.entry_high is not None:
            lines.append(f"🎯 ناحیه‌ی ورود: {fp(s.entry_low)} تا {fp(s.entry_high)}")
        elif s.entries and len(s.entries) > 1:
            parts = [f"{fp(e['price'])}" + (f" ({e['portion'] * 100:.0f}٪)" if e.get("portion") else "") for e in s.entries]
            lines.append("🎯 ورود پله‌ای: " + " | ".join(parts))
        elif s.entry is not None:
            lines.append(f"🎯 ورود: {fp(s.entry)}" + (" (قیمت لحظه‌ی اعلام)" if s.entry_is_market else ""))
        elif s.order_type == "MARKET":
            lines.append("🎯 ورود: قیمت فعلی بازار")
        lines.append(f"🛑 حد ضرر: {fp(s.sl)}" + (" (محاسبه از پیپ)" if s.pip_converted else ""))
        if s.tps:
            lines.append("💰 " + " | ".join(f"TP{i + 1}: {fp(t)}" for i, t in enumerate(s.tps)))
        elif s.tp_open:
            lines.append("💰 تارگت: باز (به اعلام تحلیلگر)")
        else:
            lines.append("💰 تارگت: اعلام نشد")
        if s.timeframe:
            lines.append(f"⏱ تایم‌فریم: {s.timeframe}")
        lines.append(f"📊 اطمینان استخراج: {s.confidence:.0%}")
        if s.evidence:
            lines.append(f"🗣 «{s.evidence[:250]}»")
        if any("بازار" in n and "بسته" in n for n in s.notes):
            lines.append("⚠️ بازار در زمان اعلام بسته است")
        if admin and s.notes:
            lines.append("📝 " + " | ".join(s.notes)[:400])
        lines.append(f"🆔 {trade_id} · {clock.hhmmss(s.spoken_at or s.created_at)}")
        if admin:
            return "\n".join(lines)
        custom = self._render_template(s, trade_id)
        if custom:
            return custom + "\n" + DISCLAIMER
        return "\n".join(lines + [DISCLAIMER])

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for t in self.trades.values():
            by_status[t.status] = by_status.get(t.status, 0) + 1
        return {"by_status": by_status, "drafts": list(self.drafts), **self.stats}
