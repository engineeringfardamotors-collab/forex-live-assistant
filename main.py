"""نقطه‌ی ورود برنامه.

حالت‌های اجرا:
  python main.py                 → شنود تلگرام + داشبورد
  python main.py --join <URL>    → باز کردن جلسه، ورود خودکار و شروع ضبط
  python main.py --probe         → آزمایش ۱۰ ثانیه‌ای ضبط تصویر و صدا
  python main.py --simulate      → تست آفلاین بدون API/تلگرام
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from pathlib import Path

from app.config import load_config
from app.dashboard import Dashboard
from app.orchestrator import SessionOrchestrator
from app.telegram_listener import run_listener
from app.utils import get_logger


async def probe(cfg: dict, seconds: int = 12, tone: bool = False) -> None:
    """بررسی سریع اینکه ضبط تصویر و صدای سیستم کار می‌کند."""
    from app.capturers import AudioCapture, ScreenCapture

    log = get_logger("probe", cfg["logging"]["level"], cfg["logging"]["dir"])
    pcfg = copy.deepcopy(cfg)
    pcfg["capture"]["audio_chunk_sec"] = 3
    pcfg["capture"]["screenshot_interval_sec"] = 3

    out = Path("session_data") / "probe"
    chunks: list[Path] = []

    async def on_chunk(p: Path) -> None:
        chunks.append(p)

    screen = ScreenCapture(pcfg, out / "frames")
    audio = AudioCapture(pcfg, out / "audio", on_chunk)

    if tone:
        import threading

        def _play() -> None:
            try:
                import numpy as np
                import soundcard as sc

                rate = 16000
                t = np.linspace(0, max(3, seconds - 2), int(rate * max(3, seconds - 2)), endpoint=False)
                wave = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
                sc.default_speaker().play(wave, samplerate=rate)
            except Exception as e:  # noqa: BLE001
                print("پخش تُن آزمایشی ناموفق بود:", e)

        threading.Thread(target=_play, daemon=True).start()

    log.info("شروع آزمایش %d ثانیه‌ای%s...", seconds, " (با تُن آزمایشی)" if tone else "")
    screen.start()
    audio.start()
    await asyncio.sleep(seconds)
    screen.stop()
    audio.stop()
    await asyncio.sleep(0.8)

    frames = screen.latest_frames(100)
    print("\n=== نتیجه‌ی آزمایش ===")
    print(f"فریم‌های تصویر : {len(frames)}  → {out / 'frames'}")
    print(f"بک‌اند صدا      : {audio.backend}")
    print(f"قطعه‌های صوتی   : {len(chunks)}  (سطح آخر: {audio.last_level:.4f})")
    if audio.last_level < 0.001:
        print("⚠️ صدا تقریباً بی‌صدا بود. مطمئن شوید در حین آزمایش صدایی از بلندگو پخش می‌شود.")
    else:
        print("✔ ضبط صدا سیگنال دریافت کرد.")


async def check_ai(cfg: dict) -> None:
    """بررسی اعتبار کلید API و فهرست مدل‌های موجود."""
    import httpx

    ai = cfg["ai"]
    prov = ai.get("provider", "openai")
    p = ai.get(prov, {})
    base = (p.get("base_url") or "").rstrip("/")
    key = p.get("api_key") or ""
    print(f"provider : {prov}")
    print(f"base_url : {base}")
    print(f"api_key  : {'تنظیم شده (' + str(len(key)) + ' کاراکتر)' if key else 'خالی'}")

    if not key:
        print("\n❌ کلید خالی است. آن را در .env یا config.yaml بگذارید.")
        return
    if not base:
        print("\n❌ base_url خالی است.")
        return

    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{base}/models", headers=headers)
        except Exception as e:  # noqa: BLE001
            print(f"\n❌ اتصال ناموفق: {type(e).__name__}: {e}")
            return

    print(f"\nGET {base}/models → {r.status_code}")
    if r.status_code != 200:
        print("❌ کلید پذیرفته نشد:", r.text[:300])
        return

    ids = [m.get("id") for m in r.json().get("data", [])]
    print(f"✅ کلید معتبر است. تعداد مدل‌ها: {len(ids)}")
    for mid in ids[:60]:
        print("   -", mid)


async def check_net(cfg: dict) -> None:
    """بررسی اتصال به تلگرام (Bot API و MTProto) و سرویس AI، با و بدون پروکسی تنظیم‌شده."""
    import socket

    import httpx

    proxy = (cfg.get("network") or {}).get("proxy") or None
    print(f"پروکسی تنظیم‌شده: {proxy or '— (اتصال مستقیم/TUN)'}")
    try:
        socket.create_connection(("149.154.167.51", 443), timeout=6).close()
        print("✅ MTProto تلگرام (شنود گروه): در دسترس")
    except OSError as e:
        print(f"❌ MTProto تلگرام: {e} — فیلترشکن را در حالت TUN روشن کنید یا network.proxy را تنظیم کنید")
    token = cfg["telegram"].get("bot_token", "")
    try:
        async with (httpx.AsyncClient(timeout=10, proxy=proxy) if proxy else httpx.AsyncClient(timeout=10)) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        print(f"✅ Bot API: {r.json().get('result', {}).get('username')}" if r.is_success else f"❌ Bot API: {r.status_code}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ Bot API: {type(e).__name__}: {e}")
    await check_ai(cfg)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Forex Live Assistant")
    parser.add_argument("--join", metavar="URL", help="باز کردن جلسه و شروع ضبط")
    parser.add_argument("--probe", action="store_true", help="آزمایش ضبط تصویر و صدا")
    parser.add_argument("--tone", action="store_true", help="پخش تُن آزمایشی در حین probe")
    parser.add_argument("--check-ai", action="store_true", help="بررسی کلید و مدل‌های API")
    parser.add_argument("--login", action="store_true", help="ورود یک‌بارهٔ حساب تلگرام")
    parser.add_argument("--list-chats", action="store_true", help="نمایش گروه‌ها و شناسه‌ها")
    parser.add_argument("--sms", action="store_true", help="درخواست کد ورود از طریق پیامک")
    parser.add_argument("--send-code", action="store_true", help="مرحله ۱: ارسال کد ورود")
    parser.add_argument("--sign-in", metavar="CODE", help="مرحله ۲: تکمیل ورود با کد")
    parser.add_argument("--find-chat", metavar="TEXT", help="جست‌وجوی گروه/کانال با نام")
    parser.add_argument("--simulate", action="store_true", help="تست آفلاین")
    parser.add_argument("--no-dashboard", action="store_true", help="بدون داشبورد")
    parser.add_argument("--replay", metavar="DIR", help="اجرای دوباره‌ی جلسه‌ی ضبط‌شده و سنجش دقت")
    parser.add_argument("--cached", action="store_true", help="در replay از پاسخ‌های ضبط‌شده‌ی مدل استفاده کن")
    parser.add_argument("--label-template", metavar="DIR", help="ساخت پیش‌نویس labels.json برای یک جلسه")
    parser.add_argument("--check-net", action="store_true", help="بررسی دسترسی به تلگرام و API")
    parser.add_argument("--pick-region", action="store_true", help="انتخاب ناحیه‌ی چارت با ماوس و ذخیره در config")
    parser.add_argument("--region-rect", metavar="X,Y,W,H", help="به‌جای انتخاب با ماوس؛ مختصات مستقیم")
    parser.add_argument("--monitor", type=int, metavar="N", help="شماره‌ی مانیتور (پیش‌فرض: مانیتورِ مرورگر)")
    args = parser.parse_args()

    cfg = load_config()
    log = get_logger("main", cfg["logging"]["level"], cfg["logging"]["dir"])

    if args.pick_region:
        from app.region_picker import pick_region

        sys_exit = pick_region(Path("config.yaml"), Path("session_data"),
                               rect=args.region_rect, monitor_index=args.monitor)
        raise SystemExit(sys_exit)

    if args.replay:
        import json as _json

        from app.replay import replay

        report = await replay(cfg, Path(args.replay), cached=args.cached)
        print(_json.dumps(report, ensure_ascii=False, indent=1, default=str))
        return

    if args.label_template:
        from app.replay import label_template

        print("ساخته شد:", label_template(Path(args.label_template)))
        return

    if args.check_net:
        await check_net(cfg)
        return

    if args.simulate:
        from app.simulation import run_simulation

        await run_simulation()
        return

    if args.probe:
        await probe(cfg, tone=args.tone)
        return

    if args.check_ai:
        await check_ai(cfg)
        return

    if args.send_code:
        from app.telegram_listener import send_code_step

        await send_code_step(cfg)
        return

    if args.sign_in:
        from app.telegram_listener import sign_in_step

        await sign_in_step(cfg, args.sign_in)
        return

    if args.login:
        from app.telegram_listener import interactive_login

        await interactive_login(cfg, force_sms=args.sms)
        return

    if args.list_chats:
        from app.telegram_listener import list_chats

        await list_chats(cfg)
        return

    if args.find_chat:
        from app.telegram_listener import find_chats

        await find_chats(cfg, args.find_chat)
        return

    log.info("Forex Live Assistant در حال راه‌اندازی است")
    log.warning("سیگنال‌ها توصیه‌ی سرمایه‌گذاری نیستند. ضبط جلسه فقط با اجازه‌ی برگزارکننده.")

    orchestrator = SessionOrchestrator(cfg)
    orchestrator.loop = asyncio.get_running_loop()

    dashboard = None
    if not args.no_dashboard:
        dashboard = Dashboard(orchestrator, cfg)
        dashboard.start()
        log.info("داشبورد روی http://%s:%s فعال است", dashboard.host, dashboard.port)

    try:
        if args.join:
            msg = await orchestrator.start_manual(args.join)
            log.info(msg)
            log.info("برای پایان، Ctrl+C را بزنید.")
            while orchestrator.in_meeting:
                await asyncio.sleep(2)
        else:
            await run_listener(cfg, orchestrator.on_meeting_link)
    except KeyboardInterrupt:
        log.info("توقف توسط کاربر")
    finally:
        try:
            await orchestrator.end_meeting("توقف برنامه")
        except (KeyboardInterrupt, asyncio.CancelledError):
            # خروج سخت حین پاک‌سازی: پرچم «پایان تمیز» معاملات را هم‌زمان (sync) بنویس
            orchestrator.trades.close_session()
        try:
            await orchestrator.publisher.flush_once()
        except Exception:  # noqa: BLE001
            pass
        orchestrator.publisher.stop()
        if dashboard:
            dashboard.stop()


if __name__ == "__main__":
    asyncio.run(main())
