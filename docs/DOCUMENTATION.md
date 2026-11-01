# مستندات کامل دستیار جلسات لایو فارکس (Forex Live Assistant)

نسخه: **2.3.0** — تاریخ: 2026-11-01 — `PROMPT_VERSION = live-v2.1`

این سند مرجع کامل پروژه است: هدف، نصب، معماری، توضیح فایل‌به‌فایل، قراردادهای داده،
پیام‌ها، پیکربندی، عیب‌یابی و فهرست کارهای باقی‌مانده.

---

## ۱. هدف و رفتار کلی

برنامه روی کامپیوتر ویندوز شما اجرا می‌شود و:

1. گروه تلگرام منبع را می‌پاید؛ به‌محض دیدن لینک جلسه (Zoom / Google Meet / Teams / Webex / پلتفرم‌های فارسی)،
   مرورگر را باز می‌کند و وارد جلسه می‌شود.
2. هم‌زمان **صدای سیستم** (گفتار تحلیلگر) و **تصویر ناحیه‌ی چارت** را ضبط می‌کند.
3. هر ۳ ثانیه تصویر را با فریم قبل مقایسه می‌کند و هر ۱۰ ثانیه قطعه‌ی صوتی را به متن تبدیل می‌کند.
   هر تغییر محلی چارت یا هر جمله‌ی حاوی اصطلاح معاملاتی، یک تحلیل مدل بینایی را فعال می‌کند.
4. مدل بینایی از تصویر + متن گفتار، «رویداد معاملاتی» با `intent` مشخص استخراج می‌کند.
5. رویداد از فیلترهای قطعی (قواعد) و اعتبارسنجی عددی می‌گذرد؛ اگر مطمئن بود، سیگنال به کانال می‌رود،
   اگر مشکوک بود به بازبینی ادمین، و اگر بی‌پایه بود رد می‌شود.
6. پس از ارسال، دستورهای مدیریتی (ریسک‌فری، سیو سود، بستن، لغو) به‌صورت **Reply** زیر همان سیگنال می‌آیند و
   اصلاحیه‌ها همان پیام را **ویرایش** می‌کنند.
7. کل جلسه ضبط می‌شود تا بعداً Replay و سنجش دقت (precision / recall / latency) انجام شود.

قاعده‌ی طلایی طراحی: **سیگنال غلط خیلی بدتر از سیگنال ازدست‌رفته است.** هر جای زنجیره که شک باشد،
سیگنال یا نمی‌رود یا با برچسب «نیاز به تأیید» می‌رود.

---

## ۲. نصب

### پیش‌نیاز
- Windows 10/11، پایتون 3.15 (فعلی) — یا 3.12 برای MT5 (بخش ۱۵)
- Chrome یا Edge نصب‌شده (برای جلسه‌ها)
- اتصال به تلگرام (فیلترشکن در حالت TUN یا `network.proxy`)

### نصب
```powershell
cd "C:\Users\Ehsan\Documents\Default Project\forex-live-assistant"
$env:PYTHONUTF8='1'
python -m pip install -r requirements.txt
python -m playwright install chromium        # برای ورود خودکار به جلسه
```

### تنظیم `.env`
کلیدهای حساس در `.env` (کنار `main.py`) قرار می‌گیرند و بر `config.yaml` اولویت دارند:

```
BOT_TOKEN=...
TG_API_ID=...
TG_API_HASH=...
AVALAI_API_KEY=...
```

### ورود یک‌باره‌ی تلگرام (شنود گروه)
```powershell
python main.py --send-code          # کد به تلگرام می‌آید
python main.py --sign-in 12345      # کد را بدهید
```
فایل `assistant_listener.session` ساخته می‌شود و دیگر لازم نیست تکرار شود.

### بررسی اتصال
```powershell
python main.py --check-net
```
خروجی سه خط: پروکسی، MTProto (شنود گروه)، Bot API، سپس مدل‌های AI.

---

## ۳. دستورهای اجرا (CLI)

| دستور | کار |
|---|---|
| `python main.py` | اجرای اصلی: داشبورد + شنود گروه تلگرام |
| `python main.py --join <URL>` | شروع دستی یک جلسه با لینک |
| `python main.py --probe [--tone]` | آزمایش ۱۲ ثانیه‌ای ضبط تصویر/صدا |
| `python main.py --pick-region` | انتخاب ناحیه‌ی چارت با ماوس و ذخیره در `config.yaml` |
| `python main.py --pick-region --region-rect 100,100,1200,700` | ذخیره‌ی مختصات بدون ماوس |
| `python main.py --pick-region --monitor 2` | انتخاب روی مانیتور دوم |
| `python main.py --check-net` | تست تلگرام و AI |
| `python main.py --check-ai` | تست کلید و فهرست مدل‌ها |
| `python main.py --simulate` | تست آفلاین کل جریان تولید پیام |
| `python main.py --replay <dir>` | اجرای دوباره‌ی جلسه‌ی ضبط‌شده با مدل واقعی |
| `python main.py --replay <dir> --cached` | همان، با پاسخ‌های ذخیره‌شده (رایگان) |
| `python main.py --label-template <dir>` | ساخت پیش‌نویس `labels.json` برای سنجش دقت |
| `python main.py --list-chats` / `--find-chat <متن>` | نمایش شناسه‌ی گروه‌ها |
| `python main.py --no-dashboard` | بدون داشبورد |
| `python tests/test_scenarios.py` | ۳۶ تست آفلاین |
| `python tests/test_extra.py` | ۴۱ تست تکمیلی آفلاین (۲.۲.۰ + ۲.۳.۰) |
| `python -m app.test_live` | تست واقعی مدل بینایی با چارت مصنوعی |

---

## ۴. معماری

```
┌──────────────────────── Telegram ────────────────────────┐
│ TelegramLinkListener (Telethon)  →  لینک جلسه → on_link  │
└───────────────────────────┬──────────────────────────────┘
                            ▼
                 SessionOrchestrator.start_manual(url)
        ┌───────────────────┼────────────────────┬─────────────────┐
        ▼                   ▼                    ▼                 ▼
   AudioCapture        ChartWatcher         CaptureHealth    SessionRecorder
 (WASAPI loopback)   (موتور اصلی)         (هشدار سلامت)     (ضبط کامل)
        │                   │
        │                   ├─ FrameAnalyzer  ← mss (ناحیه‌ی چارت)
        │                   ├─ AIProvider.extract_live_events (بینایی)
        │                   ├─ signal_model.build_signal / validate / route
        │                   ├─ TradeBook (ماشین حالت + پیام‌ها)
        │                   ├─ MarketPriceProvider (MT5/http/chart)
        │                   └─ LatencyTracker (تفکیک تأخیر)
        ▼
  AIProvider.transcribe (STT)  →  lexicon.normalize / merge_overlap → ChartWatcher.add_transcript
        │
        └── TelegramPublisher (صف پایدار دیسک) → کانال سیگنال / چت ادمین
```

قاعده‌ی مهم: **تنها TradeBook حق ارسال پیام سیگنال را دارد.** خلاصه‌های دوره‌ای هرگز سیگنال نمی‌فرستند.

---

## ۵. ساختار فایل‌ها

```
forex-live-assistant/
├─ main.py                  نقطه‌ی ورود و CLI
├─ config.yaml              پیکربندی فعال (کلیدهای حساس در .env)
├─ config.example.yaml      نمونه‌ی پیکربندی برای انتشار
├─ requirements.txt         وابستگی‌ها
├─ .env                     رازها (منتشر نشود)
├─ README.md                راهنمای کوتاه
├─ start.bat                اجرای ساده (venv 3.12 اگر باشد)
├─ start_forever.bat        اجرای پایدار: اگر برنامه کرش کرد ۱۰ ثانیه بعد دوباره اجرا می‌شود
├─ scripts/
│  ├─ install_autostart_task.ps1    ثبت Task Scheduler: شروع خودکار با ورود به ویندوز
│  └─ uninstall_autostart_task.ps1  حذف همان تسک
├─ make_release.py          ساخت ZIP انتشار (بدون رازها)
├─ docs/DOCUMENTATION.md    همین سند
├─ app/
│  ├─ config.py             بارگذاری/ادغام config + env، DEFAULTS
│  ├─ utils.py              لاگر فایل/کنسول، now_iso
│  ├─ clock.py              زمان قابل‌تغییر (برای Replay)
│  ├─ telegram_listener.py  شنود گروه با Telethon + پروکسی + ورود مرحله‌ای
│  ├─ browser_bot.py        ورود به Zoom/Meet/Teams/Webex + تشخیص پایان جلسه
│  ├─ capturers.py          ScreenCapture، AudioCapture (loopback/sounddevice، قطعه‌های همپوشان، انتخاب دستگاه)
│  ├─ ai.py                 STT، مدل بینایی، تلاش مجدد با backoff، پیش‌فیلتر ارزان، کم‌کردن حجم تصویر
│  ├─ prompts.py            دستورالعمل کامل مدل + STT_PROMPT + سازنده‌ی پیام کاربر
│  ├─ frames.py             ناحیه‌ی تغییر، فریم سیاه/فریز، کم‌وزن‌کردن کندل جاری
│  ├─ symbols.py            نمادها، مشخصات، پیپ، تکمیل اعداد خلاصه، نام‌های بروکر
│  ├─ lexicon.py            واژه‌نامه‌ی فارسی، اصلاح STT، اعداد حرفی، تور ایمنی گفتار
│  ├─ signal_model.py       Signal، build_signal، validate، route، merge_signals
│  ├─ trade_state.py        ماشین حالت معامله، فرمت پیام (+قالب دلخواه)، Reply/Edit، دکمه‌ی تأیید، پله‌ی حجم
│  ├─ market.py             قیمت مستقل (MT5/http/chart، پیش‌تنظیم gold-api)، بازار باز/بسته، ضبط قدیمی
│  ├─ publisher.py          صف پایدار، Reply، Edit، ادمین، dry_run، پروکسی
│  ├─ latency.py            تفکیک تأخیر به مراحل + میانگین/بیشینه
│  ├─ health.py             هشدار پنجره/فریم/قفل/خواب
│  ├─ winapi.py             ctypes: DPI، پنجره‌ی کروم، قفل صفحه، جلوگیری از خواب، Suspend
│  ├─ region_picker.py      انتخاب ناحیه با ماوس + تبدیل DPI + پیش‌نمایش
│  ├─ recorder.py           ضبط جلسه (events.jsonl + فریم + صدا) + چرخش/فشرده‌سازی جلسات قدیمی
│  ├─ replay.py             Replay + معیارهای precision/recall/latency
│  ├─ live_watcher.py       موتور لحظه‌ای (ChartWatcher) — قلب برنامه
│  ├─ orchestrator.py       چرخه‌ی جلسه، ورود مجدد (reconnect)، watchdog پایان، وضعیت داشبورد
│  ├─ dashboard.py          داشبورد HTTP محلی (+ انتخاب دستگاه صدا)
│  ├─ simulation.py         تست آفلاین کل جریان
│  └─ test_live.py          تست واقعی مدل با چارت مصنوعی
├─ tests/
│  ├─ test_scenarios.py     ۳۶ تست سناریویی
│  └─ test_extra.py         ۴۱ تست تکمیلی (outbox، callback، MT5/HTTP ماک، صدا، سلامت، reconnect، ترتیب STT/انتشار، ...)
├─ logs/                    assistant.log (چرخشی)
└─ session_data/
   ├─ outbox.json           صف ارسال (ماندگار)
   ├─ sent_map.json         نگاشت شناسه‌ی محلی → message_id تلگرام
   ├─ sessions/<تاریخ>/     events.jsonl، frames/، audio/، labels.json (قدیمی‌ها zip/حذف می‌شوند)
   ├─ region_preview.png    برش ناحیه‌ی چارت
   └─ region_preview_full.png  همان + کادر قرمز روی تصویر کامل
```

---

## ۶. ماژول‌ها به‌تفصیل

### `app/live_watcher.py` — ChartWatcher
موتور اصلی. هر `scan_interval_sec` (۳ ثانیه) عکس می‌گیرد، `FrameAnalyzer.analyze` را صدا می‌زند و:

| رویداد | واکنش |
|---|---|
| تغییر محلی/سراسری معنی‌دار | تحلیل تصویری فوری |
| جمله‌ی حاوی کلیدواژه (`is_trigger`) | ست‌کردن `_speech_trigger` → تحلیل فوری با `speech_cooldown_sec` (۴ ثانیه) |
| فریم سیاه/فریز | تحلیل تصویری متوقف؛ سلامت هشدار می‌دهد |

از ۲.۳.۰ تحلیل بینایی در **تسک پس‌زمینه** اجرا می‌شود: حین ~۱۰ ثانیه‌ی فراخوانی مدل، عکس‌برداری و
تحلیل ارزان فریم‌ها ادامه دارد (سلامت/تشخیص بی‌فعالیت زنده می‌ماند)؛ رویداد چارتِ حین تحلیل «در انتظار»
می‌ماند و بلافاصله پس از اتمام تحلیل جاری (با cooldown) اجرا می‌شود. نمونه‌ی mss برای هر thread ماندگار
است و ناحیه‌ی چارت روی **مانیتور درست** (حاوی `live.region`) گرفته می‌شود.

متدهای کلیدی:
- `add_transcript(text, t0, t1, audio_file, stt_start, stt_end)` — نرمال‌سازی، حذف همپوشانی، ثبت زمان‌ها برای تأخیر.
- `process(img, reason, info)` — قفل‌گذاری، cooldown، `_process_locked`.
- `handle_model_output(data)` — به‌روزرسانی نماد/قیمت چارت، حلقه روی `events[]`.
- `_handle_event(ev, frame, frame_symbol)` — مسیردهی بر اساس `intent`.
- `_safety_issues(...)` — تور ایمنی مستقل از مدل (نفی/گذشته/فرضی/بدون گفتار/چند چارت/…).
- `stats()` — برای داشبورد و `/api/status`.

### `app/signal_model.py`
- `Signal` (dataclass): `symbol, side, order_type, intent, entry, entry_low, entry_high, entries[], sl, tps[], tp_open, condition, confidence, evidence, sources, notes, spoken_at, ...`
  متد `entry_ref()` قیمت مرجع ورود را می‌دهد؛ `core_key(spec)` برای تشخیص تکرار.
- `build_signal(event, ctx) -> (Signal|None, issues)` — نرمال‌سازی نماد، تکمیل اعداد خلاصه با جهت،
  تبدیل پیپ، استنتاج نوع سفارش، مرتب‌سازی اهداف.
- `validate(sig, spec, market_price, independent, cfg) -> issues` — قواعد بخش ۸.
- `route(sig, issues, cfg) -> "publish"|"review"|"reject"|"draft"`.
- `merge_signals(old, new)` — تکمیل سیگنال ناقص با اطلاعات جدید (آخرین عدد برنده است).

### `app/trade_state.py`
- `TradeBook` — دیکشنری معامله‌ها + پیش‌نویس‌ها + آمار.
- `handle_entry`, `handle_management`, `handle_cancel`, `on_callback`, `tick`, `format_signal`, `summary`.
- `Trade` — `id (T1,T2…)`, `signal`, `status`, `msg_id`, `last_msg_id`, `history[]`.

### `app/publisher.py`
- `TelegramPublisher` با `enqueue(text, target, reply_to, edit_of, buttons, created_at)`؛
  هرس نگاشت sent نگاشت پیام معامله‌های زنده را حفظ می‌کند (`protect_ids` از TradeBook).
- صف روی `session_data/outbox.json`؛ بازگشت بعد از قطعی اینترنت بدون گم‌شدن.
- `dry_run: true` → هیچ درخواستی به Bot API نمی‌رود، فقط لاگ.
- `on_callback` برای دکمه‌های تأیید/رد (`approve:T3`).
- `on_sent(local_id, ts)` برای اندازه‌گیری مرحله‌ی ارسال.
- `MemoryPublisher` برای تست/Replay.

### `app/ai.py`
- `transcribe()` — STT با `STT_PROMPT` (واژه‌نامه‌ی فارسی) + فیلتر خروجی‌های چینی/ژاپنی.
- `extract_live_events(image, user_text, image_url=None)` — تصویر را کوچک/JPEG می‌کند (`vision_image_max_width`) و با
  `LIVE_EXTRACTION_SYSTEM` می‌فرستد؛ اگر `image_url` از قبل آماده باشد (ChartWatcher در thread آماده می‌کند)
  تصویر دوباره از دیسک بازخوانی/کدگذاری نمی‌شود.
- `generate_analysis()` — خلاصه‌ی متنی دوره‌ای (بدون سیگنال).
- **تلاش مجدد با backoff**: همه‌ی فراخوانی‌ها (`_post_with_retry`) روی خطاهای گذرا (شبکه، 429، 500/502/503/504)
  تا `ai.max_retries` بار (پیش‌فرض ۲) با تأخیر نمایی (`ai.retry_base_sec` × 2ⁿ + jitter) دوباره تلاش می‌کنند؛
  `Retry-After` تلگرام‌مانند هم رعایت می‌شود. فریم/قطعه با یک خطای گذرا از دست نمی‌رود.
- `precheck_frame()` — پیش‌فیلتر ارزان (اختیاری): با `live.vision_prefilter.model` می‌پرسد «رسم معاملاتی
  تازه‌ای هست؟»؛ اگر نه، مدل اصلی صدا زده نمی‌شود. محرک گفتاری هرگز پیش‌فیلتر نمی‌شود و در خطا مدل اصلی می‌رود.

### `app/frames.py`
`FrameAnalyzer.analyze(img) -> FrameInfo(valid, reason, diff, changed_frac, bbox, kind, frozen_sec)`
- `blank` اگر `std < min_frame_std` (۵) → اشتراک صفحه قطع است.
- `local` / `global` / `live_edge` (لبه‌ی راست = کندل جاری، وزن ۰.۲۵).
- `hint()` جمله‌ی راهنما برای مدل: «تغییر محلی در بالا-راست تصویر، ۱۲٪ صفحه».

### `app/market.py`
`MarketPriceProvider.get_price(symbol) -> (price, source, independent)` با ترتیب `market.sources`:
`mt5` → `http` → `chart`. فقط mt5/http مستقل‌اند. `resolve_mt5_symbol` نام بروکر (XAUUSDm/GOLD/…) را
خودکار پیدا می‌کند. `market_open(symbol)` جمعه ۲۱:۰۰ تا یکشنبه ۲۱:۰۰ UTC را بسته می‌داند (کریپتو باز).
منبع HTTP دو راه دارد: الگوی دستی (`market.http.url` + `json_path` + `symbol_map`) یا پیش‌تنظیم آماده
`market.http.preset: "gold-api"` (رایگان بدون کلید؛ XAUUSD/XAGUSD/XPTUSD/XPDUSD) برای وقتی MT5 نصب نیست.
نگاشت نماد کاربر روی نقشه‌ی پیش‌فرض preset **ادغام** می‌شود، نه جایگزینی. کلاینت HTTP از ۲.۳.۰ پایاست
(بدون TLS handshake در هر فراخوانی؛ اتصال کهنه → یک تلاش مجدد با کلاینت تازه) و در پایان جلسه با `aclose()` بسته می‌شود.

### `app/health.py` + `app/winapi.py`
پایش مداوم (۵ ثانیه) و فقط هشدار: جابه‌جایی/تغییر اندازه/کوچک‌شدن پنجره، بیرون‌بودن ناحیه از پنجره،
پنجره‌ی روی چارت، `FRAME_BLACK`/`FRAME_FROZEN` بیش از ۳۰ ثانیه، قفل صفحه، Suspend/Resume.
`winapi` همچنین در طول جلسه از خواب رفتن سیستم جلوگیری می‌کند (`SetThreadExecutionState`).

### `app/region_picker.py`
انتخاب ناحیه با ماوس روی تصویر واقعی صفحه؛ ذخیره‌ی مختصات **فیزیکی** در `live.region` + `live.region_meta`
(اندازه‌ی صفحه، مقیاس، مختصات پنجره). تبدیل‌ها مستقل از DPI هستند چون نسبت از اندازه‌ی عکس و بوم محاسبه می‌شود.

### `app/latency.py`
شش مرحله: `audio_wait`, `stt`, `trigger_wait`, `vision`, `decision`, `telegram` + `total`.
هر سیگنال در `events.jsonl` با `type: "latency"` ثبت می‌شود و در داشبورد میانگین/بیشینه دارد.

### `app/replay.py`
`replay(cfg, dir, cached)` → بازپخش رویدادها با `clock.set_time`، اجرای دوباره‌ی مدل یا پاسخ‌های ذخیره‌شده،
خروجی JSON + `metrics` اگر `labels.json` باشد:
`precision = درست/ارسال‌شده`، `recall = درست/واقعی`، `latency_median_sec`، لیست `false_positives` و `missed`.

---

## ۷. جریان دقیق یک سیگنال

1. **صدا**: `AudioCapture` هر ۱۰ ثانیه قطعه‌ی WAV می‌نویسد (۲ ثانیه همپوشانی، قطعه‌های زیر `silence_peak` رد می‌شوند).
2. **STT**: متن برمی‌گردد، `normalize_trading_text` اعداد/اصطلاحات را اصلاح می‌کند، `merge_overlap` تکرار را حذف می‌کند.
3. **محرک**: اگر جمله کلیدواژه‌ی معاملاتی داشت → تحلیل فوری؛ وگرنه تغییر محلی چارت محرک است.
4. **بینایی**: تصویر + ۷۵ ثانیه گفتار اخیر + فهرست معامله‌های باز + راهنمای ناحیه‌ی تغییر + قیمت مرجع.
5. **قواعد**: `build_signal` (نماد، اعداد، پیپ، نوع سفارش) → `_safety_issues` → `validate` → `route`.
6. **انتشار**: `TradeBook.handle_entry` → پیام کانال (`enqueue`) → صف → Bot API.
7. **ثبت**: `events.jsonl` شامل transcript، analysis، decision، latency، market.

---

## ۸. اعتبارسنجی (`validate`) — کدها و معنا

| کد | شدت | معنا |
|---|---|---|
| `NO_SIDE` | fatal | جهت معلوم نیست |
| `INCOMPLETE` | fatal | ورود یا حد ضرر نیست → پیش‌نویس |
| `SL_WRONG_SIDE` | fatal | حد ضرر سمت اشتباه |
| `SL_INSIDE_ZONE` | fatal | حد ضرر داخل ناحیه‌ی ورود |
| `TP_WRONG_SIDE` | fatal | تارگت سمت اشتباه |
| `TP_ORDER` | info | ترتیب تارگت‌ها اصلاح شد |
| `SL_TOO_FAR` / `SL_TOO_CLOSE` | review | فاصله‌ی استاپ غیرعادی/خیلی کم |
| `ENTRY_FAR_FROM_MARKET` | fatal اگر منبع مستقل، وگرنه review | ورود دور از بازار (با کسر تلورانس بروکر) |
| `MARKET_ENTRY_DRIFT` | review | ورودِ بازار با قیمت فعلی فاصله دارد |
| `LIMIT_TYPE_MISMATCH` / `STOP_TYPE_MISMATCH` | review | نوع سفارش با جای ورود نمی‌خواند |
| `RR_ABNORMAL` | review | R:R بیرون از ۰.۲ تا ۲۰ |
| `PIP_AMBIGUOUS` | review | تبدیل پیپ مبهم (طلا/نقره) |
| `UNRESOLVED_NUMBER` | review | عدد خلاصه تکمیل نشد |
| `SYMBOL_MISMATCH` | review | نماد گفتار ≠ نماد چارت |
| `MULTI_CHART` / `OBSTRUCTED` | review | چند نمودار / پوشیده بودن |
| `SPEECH_NEGATION` / `SPEECH_PAST` / `SPEECH_HEDGE` | review | تور ایمنی زبان |
| `NO_SPEECH_EVIDENCE` | review | سیگنال فقط تصویری |
| `OLD_DRAWING_ONLY` | fatal | فقط رسم قدیمی، بدون گفتار |
| `SPEECH_CHART_CONFLICT` | review | تناقض عدد گفتار و تصویر |
| `NOT_ANALYST` | review | گوینده تحلیلگر نیست |
| `MARKET_CLOSED` | review | بازار بسته |
| `STALE_STREAM` | review | احتمال پخش ضبط قدیمی |

**route**: هر fatal → `reject`؛ `INCOMPLETE` → `draft`؛ هر review یا `confidence < auto_publish_confidence` (۰.۸) → `review`؛
`confidence < min_confidence` (۰.۴) → `reject`؛ بقیه → `publish`.

---

## ۹. `intent` — چه چیزی ارسال می‌شود

| intent | ارسال؟ |
|---|---|
| `ACTUAL_ENTRY` | ✅ بلافاصله (اگر اعتبارسنجی پاک بود) |
| `PENDING_ORDER` | ✅ بلافاصله |
| `MANAGEMENT` | ✅ به‌صورت Reply روی سیگنال اصلی |
| `CANCEL` | ✅ Reply «لغو شد» |
| `CONDITIONAL` | ⚠️ با برچسب «سیگنال شرطی» و متن شرط |
| `HYPOTHETICAL` | ❌ فقط ثبت |
| `REVIEW_PAST` | ❌ فقط ثبت |
| `BIAS_ONLY` | ❌ فقط ثبت |
| `NOT_A_SIGNAL` | ❌ |

---

## ۱۰. ماشین حالت معامله

```
DRAFT ──(کامل شد)──► SENT ──(قیمت به ورود رسید)──► ACTIVE
  │                   │                              │
(انقضا)             (لغو)                  ┌────────┼────────┐
  ▼                   ▼                    ▼        ▼        ▼
EXPIRED           CANCELLED            BREAKEVEN  TP_HIT   SL_HIT
                                            │        │
                                         PARTIAL   CLOSED
REVIEW ──(تأیید ادمین)──► SENT    REVIEW ──(رد)──► REJECTED / (بی‌پاسخ)──► EXPIRED
```

- **تکرار**: همان نماد + جهت + ورود و «فاصله‌ی ورود < ۰.۱٪ و حداکثر ۳۰ دقیقه از آخرین فعالیت» → `duplicate`
  (ارسال نمی‌شود). دستورهای مدیریتی به معامله‌ی باز هر چقدر هم قدیم وصل می‌شوند (ملاک: وضعیت).
- **اصلاح**: همان شرایط ولی مقادیر متفاوت → `updated`: پیام کانال **ویرایش** می‌شود با «✏️ اصلاح شد» + یک Reply با خلاصه‌ی
  تغییرات.
- **پیش‌نویس**: سیگنال ناقص ۱۲۰ ثانیه نگه داشته می‌شود تا با فریم/گفتار بعدی کامل شود؛ اگر نشد، به ادمین گزارش و حذف.
- **ری‌استارت**: وضعیت معاملات در `session_data/trades.json` ذخیره (اتمیک، مقاوم به قفل آنتی‌ویروس، با
  `saved_at`) و در شروع برنامه: اجرای قبلی با **پایان تمیز جلسه** → بازیابی نمی‌شود (پرچم قبل از هر await
  نوشته می‌شود تا خروج سخت هم از دست نرود)؛ **کرش** → همه‌ی معامله‌های زنده بازیابی می‌شوند، اما فقط اگر
  فایل تازه‌تر از `trades.restore_max_gap_min` (۱۲۰ دقیقه) باشد — چون بستن کنسول/خاموشی ویندوز/قطع برق هم
  «کرش» حساب می‌شود و سیگنال دیروز نباید مبنای آپدیت امروز شود (هشدار + حفظ شماره‌ی شناسه‌ها).
  در جلسه‌ی فعال، حلقه‌ی پایش هر ~۱۵ ثانیه `tick` را صدا می‌زند که وضعیت را ذخیره می‌کند — پس
  `saved_at` حتی بدون تغییر معاملاتی تازه می‌ماند (ذخیره در `finally` تیک تضمین شده) و ری‌استارت
  فوری در جلسه‌ی طولانی معامله‌های باز را از دست نمی‌دهد؛ ذخیره‌ی عقب‌افتاده پس از پایان جلسه
  نمی‌تواند پرچم `session_ended` را خنثی کند.
- **بازبینی**: اگر `admin_chat` تنظیم شده باشد، پیام با دکمه‌های «✅ تأیید و ارسال / ❌ رد» به ادمین می‌رود؛ پس از ۵ دقیقه منقضی می‌شود.
  بدون `admin_chat` و با `review_fallback: label` همان سیگنال با تیتر «⚠️ نیاز به تأیید — با احتیاط» در کانال منتشر می‌شود.

### نمونه پیام سیگنال
```
⚡ سیگنال XAUUSD — 🟢 خرید (Buy Limit)
🎯 ورود: 3985.50
🛑 حد ضرر: 3978.00
💰 TP1: 3995.00 | TP2: 4005.00
⏱ تایم‌فریم: M15
📊 اطمینان استخراج: 96%
🗣 «…ورود ۳۹۸۵ و نیم، استاپ ۳۹۷۸…»
🆔 T7 · 12:41:05
ℹ️ استخراج خودکار از جلسه‌ی زنده؛ ممکن است خطا داشته باشد. توصیه‌ی سرمایه‌گذاری نیست.
```

### نمونه Reply مدیریتی
```
XAUUSD · T7
🛡 ریسک‌فری: حد ضرر را به نقطه‌ی ورود منتقل کنید
حد ضرر جدید: 3985.50
🗣 «ریسک فری کنید»
```

---

## ۱۱. نمادها و اعداد (`symbols.py`)

- نام‌های بروکری: `XAUUSDm`, `XAUUSD.m`, `GOLD`, `USTEC`, `DJ30`, `US100` … به نام استاندارد نگاشت می‌شوند.
- فارسی: «طلا/انس/گلد» → XAUUSD، «یورو دلار» → EURUSD، «داوجونز» → US30 …
- `expand_short_price(85, ref=3982.4, gold)` → `3985`؛ `(85.5)` → `3985.5`؛ برای اهداف «۹۵، ۵» با جهت معامله → `3995`، `4005`.
- ارقام فارسی/عربی، «و نیم»، جداکننده‌ی هزارگان، «۱,۰۸۵۰» اروپایی → عدد درست.
- **پیپ**: طلا/نقره `pip_ambiguous` → تبدیل پیپ به بازبینی می‌رود، نه ارسال مستقیم.
- **تلورانس بروکر**: `SymbolSpec.broker_tolerance` (طلا ۳.۰ دلار، فارکس ۳ پیپ) از فاصله‌ی «ورود از بازار» کسر می‌شود
  تا اختلاف ۱ تا ۳ دلاری بین بروکر تحلیلگر و MT5 شما هشدار ایجاد نکند.

## ۱۲. واژه‌نامه و تور ایمنی گفتار (`lexicon.py`)

- اصلاح خطاهای STT: «سر لیمیت» → «سل لیمیت»، «تی پی» یکسان‌سازی …
- اعداد حرفی: «سه هزار و نهصد و هشتاد و پنج و نیم» → `3985.5`.
- `speech_flags`: «نخرید/نگیرید» → NEGATION، «اگر/شاید/فرض کنید» → HEDGE، «دیروز/قبلاً» → PAST.
- `merge_overlap("ورود سه هزار و نهصد", "و نهصد و هشتاد")` → `"و هشتاد"`.

## ۱۳. صدا

- `capture.audio_chunk_sec: 10` (قطعه)، `audio_overlap_sec: 2` (همپوشانی)، `silence_peak: 0.001`.
- مسیر اصلی `soundcard` (WASAPI loopback)؛ مسیر پشتیبان `sounddevice` — **هر دو مسیر همپوشانی و
  زمان‌بندی قطعه (`chunk_times`) دارند** تا عددی که بین دو قطعه می‌افتد گم نشود.
- `capture.audio_device` (نام دستگاه — بخشی از نام کافی است — یا ایندکس ورودی) دستگاه ضبط را انتخاب می‌کند؛
  خالی = خودکار (بلندگوی پیش‌فرض). تعویض حین جلسه از داشبورد (`POST /api/audio-device`) هم ممکن است.
- `STT_PROMPT` واژه‌نامه‌ی فارسی را به مدل تبدیل گفتار می‌دهد تا «بای/سل/پیپ» درست نوشته شوند.
- همپوشانی باعث می‌شود عددی که بین دو قطعه می‌افتد گم نشود؛ `merge_overlap` تکرار را حذف می‌کند.
- «آخرین عدد برنده است»: `merge_signals` مقدار جدید را جایگزین قبلی می‌کند (اصلاح تحلیلگر).

## ۱۴. فریم‌ها و ناحیه‌ی چارت

- `min_frame_std: 5` → فریم سیاه/یکدست نامعتبر.
- `frozen_after_sec: 90` (پایش) و `health.frozen_after_sec: 30` (هشدار).
- `live.region` = ناحیه‌ی چارت (پیکسل فیزیکی). اگر ناحیه بیرون صفحه باشد، کل صفحه گرفته می‌شود و به ادمین گزارش می‌شود.
- `region_meta` (اندازه‌ی صفحه/مقیاس/مختصات پنجره) برای تشخیص جابه‌جایی پنجره استفاده می‌شود.

## ۱۵. MT5 (قیمت مستقل)

وضعیت فعلی: **پکیج `MetaTrader5` برای Python 3.15 ویل ندارد** (نصب مستقیم خطای «No matching distribution» داد)
و طبق تصمیم شما نصب اجباری انجام نشد. راه‌های جایگزین تا راه‌اندازی MT5:

- **منبع HTTP رایگان**: `market.http.preset: "gold-api"` و `market.sources: ["http", "chart"]` — بدون کلید،
  برای XAUUSD/XAGUSD (کد: `app/market.py` پیش‌تنظیم‌ها).
- بدون قیمت مستقل، «ورود دور از بازار» فقط به بازبینی می‌فرستد و تشخیص ضبط قدیمی (`STALE_STREAM`) کار نمی‌کند.

راه‌حل اصلی (بدون تغییر پایتون فعلی): محیط مجازی ۳.۱۲ فقط برای اجرای برنامه:

```powershell
cd "C:\Users\Ehsan\Documents\Default Project\forex-live-assistant"
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install --upgrade pip
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
.\.venv312\Scripts\python.exe -m pip install MetaTrader5
# تست:
.\.venv312\Scripts\python.exe -c "import MetaTrader5 as m; print(m.initialize(), m.terminal_info())"
# اجرا:
.\.venv312\Scripts\python.exe main.py
```
سپس ترمینال MT5 مورد استفاده‌ی بروکر را باز بگذارید. اگر چند ترمینال دارید، مسیر را در `market.mt5_path` بگذارید
(مثلاً `C:\Program Files\FxPro - MetaTrader 5\terminal64.exe`). نام نماد خودکار پیدا می‌شود؛ اگر نشد:
`market.broker_symbols: {XAUUSD: "XAUUSDm"}`.

## ۱۶. تأخیر (Latency)

نتیجه‌ی اندازه‌گیری واقعی (تست ۲۰۲۶-۱۰-۳۱، چارت مصنوعی، مدل `gpt-5.6-sol`):

| مرحله | نمونه |
|---|---|
| انتظار قطعه‌ی صوتی | ۱۰.۰ ثانیه |
| تبدیل گفتار به متن | ۰.۵–۳ ثانیه |
| انتظار تا تحلیل | ۰–۱ ثانیه |
| مدل بینایی | ۹.۸ ثانیه (قبلاً ۱۶ ثانیه با PNG کامل) |
| اعتبارسنجی و تصمیم | ۰.۰۲ ثانیه |
| ارسال تلگرام | ۰.۳–۲ ثانیه |
| **کل** | **~۲۰ ثانیه** |

بزرگ‌ترین سهم‌ها: **مدل بینایی** و بعد **انتظار قطعه‌ی صوتی**. کاهش‌های انجام‌شده:
کوچک‌کردن تصویر به ۱۴۰۰ پیکسل و JPEG q92 (حدود ۴۰٪ سریع‌تر)؛ از ۲.۳.۰ کدگذاری PNG/JPEG و تحلیل ارزان
فریم در thread اجرا می‌شوند (توقف event loop حین ارسال حذف شد)، تحلیل بینایی حلقه‌ی پایش را بلاک نمی‌کند
(رویداد حین تحلیل گم نمی‌شود) و کلاینت HTTP قیمت پایاست. کاهش‌های ممکن در آینده:
`audio_chunk_sec: 8` (کمی سریع‌تر، هزینه بیشتر)، یا مدل بینایی سریع‌تر/کوچک‌تر برای پیش‌فیلتر (بخش ۲۸).

## ۱۷. سلامت ضبط

کدهای هشدار: `WINDOW_MINIMIZED`, `WINDOW_MOVED`, `WINDOW_MISSING`, `REGION_OUTSIDE`, `OCCLUDED`,
`FRAME_BLACK`, `FRAME_FROZEN`, `SCREEN_LOCKED`, `SLEEP`, `SLEEP_GUARD_OFF`.
هر هشدار در داشبورد و لاگ و (اگر `admin_chat` باشد) در تلگرام دیده می‌شود. **هیچ‌کدام جلسه را نمی‌بندند.**

## ۱۸. داشبورد

`http://127.0.0.1:8765`

| مسیر | کار |
|---|---|
| `/` | صفحه‌ی وضعیت، هشدارهای سلامت، جدول تأخیر، شروع/پایان جلسه، انتخاب دستگاه صدا، تست آفلاین |
| `GET /api/status` | JSON کامل: وضعیت جلسه، آمار watcher، معامله‌ها، صف، سلامت، منبع قیمت، دستگاه صدا، dry_run |
| `GET /api/audio-devices` | فهرست دستگاه‌های loopback و ورودی صدا |
| `POST /api/audio-device` | `{"device": "نام دستگاه یا null"}` — تعویض حین جلسه بدون ری‌استارت |
| `POST /api/start` | `{"url": "..."}` |
| `POST /api/end` | پایان جلسه |
| `POST /api/simulate` | اجرای `app/simulation.py` |

داشبورد فقط روی 127.0.0.1 گوش می‌دهد (بدون احراز هویت).

## ۱۹. ضبط، Replay، دقت

هر جلسه در `session_data/sessions/<YYYYmmdd_HHMMSS>/`:
`events.jsonl` (transcript / analysis / decision / latency / market / meta)، `frames/`، `audio/`.

```powershell
python main.py --label-template session_data\sessions\20261101_120000   # ساخت labels.template.json
# آن را به labels.json تبدیل و اصلاح کنید (سیگنال‌های واقعی جلسه)
python main.py --replay session_data\sessions\20261101_120000            # با مدل واقعی
python main.py --replay session_data\sessions\20261101_120000 --cached   # ارزان (تست قواعد)
```

---

## ۲۰. مرجع کامل پیکربندی

| کلید | پیش‌فرض | توضیح |
|---|---|---|
| `telegram.bot_token` | — | از BotFather (یا `BOT_TOKEN` در .env) |
| `telegram.signal_channel` | — | شناسه‌ی کانال سیگنال |
| `telegram.api_id` / `api_hash` / `phone` | — | حساب شنونده (Telethon) |
| `telegram.session_name` | `assistant_listener` | فایل سشن |
| `telegram.watch_groups` | `[-100… (گروه منبع)]` | گروه‌های منبع لینک |
| `telegram.admin_chat` | `""` | چت ادمین برای خطا/بازبینی (خالی = برچسب در کانال) |
| `network.proxy` | `""` | `socks5://…` یا `http://…`؛ خالی = TUN/مستقیم |
| `publisher.dry_run` | `true` | **true = هیچ پیامی به کانال نمی‌رود** |
| `publisher.delay_note_sec` | `60` | بالاتر از این → «با تأخیر …» در پیام |
| `publisher.drop_after_sec` | `3600` | پیام کهنه‌تر از این به کانال نمی‌رود |
| `health.check_interval_sec` | `5` | فاصله‌ی پایش |
| `health.black_after_sec` | `30` | آستانه‌ی هشدار فریم سیاه |
| `health.frozen_after_sec` | `30` | آستانه‌ی هشدار فریز |
| `health.move_tolerance_px` | `8` | تلورانس جابه‌جایی پنجره |
| `health.prevent_sleep` | `true` | جلوگیری از خواب/خاموشی نمایشگر |
| `meeting.use_browser` | `true` | ورود خودکار با Playwright |
| `meeting.user_data_dir` | `./browser_profile` | پروفایل ماندگار کروم |
| `meeting.browser_channel` | `""` | `chrome` / `msedge` / خالی = خودکار |
| `meeting.join_timeout_sec` | `45` | مهلت ورود |
| `meeting.manual_wait_sec` | `600` | انتظار ورود دستی |
| `meeting.reconnect.enabled` | `true` | قطعی جلسه → ورود مجدد خودکار |
| `meeting.reconnect.max_attempts` | `3` | حداکثر تلاش (سهمیه پس از ۱۰ دقیقه پایداری تازه می‌شود) |
| `meeting.reconnect.wait_sec` | `20` | انتظار پیش از هر تلاش مجدد |
| `meeting.reconnect.manual_join_wait_sec` | `45` | مهلت ورود دستی در تلاش مجدد |
| `capture.audio_sample_rate` | `16000` | نرخ نمونه |
| `capture.audio_chunk_sec` | `10` | طول قطعه (تأخیر ↔ هزینه) |
| `capture.audio_overlap_sec` | `2` | همپوشانی قطعه‌ها (هر دو مسیر soundcard و sounddevice) |
| `capture.silence_peak` | `0.001` | آستانه‌ی سکوت |
| `capture.audio_device` | `""` | نام دستگاه ضبط (بخشی از نام کافی است) یا ایندکس؛ خالی = خودکار |
| `ai.provider` | `avalai` | `avalai` / `sinox` / `openai` |
| `ai.<provider>.stt_model` | `gpt-4o-transcribe` | تبدیل گفتار |
| `ai.<provider>.vision_model` | `gpt-5.6-sol` | مدل بینایی |
| `ai.<provider>.max_retries` | `2` | تلاش مجدد خطاهای گذرا (شبکه/429/5xx) با backoff |
| `ai.<provider>.retry_base_sec` | `1.5` | پایه‌ی تأخیر نمایی تلاش مجدد |
| `analysis.analysis_interval_min` | `15` | خلاصه‌ی دوره‌ای (۰=خاموش) |
| `analysis.publish_summaries` | `true` | ارسال خلاصه در کانال |
| `analysis.auto_end_idle_min` | `8` | پایان خودکار پس از بی‌فعالیتی |
| `analysis.max_session_hours` | `4` | سقف جلسه |
| `live.scan_interval_sec` | `3` | فاصله‌ی عکس |
| `live.change_threshold` | `1.8` | حساسیت تغییر |
| `live.cell_threshold` | `12` | آستانه‌ی سلول تغییر |
| `live.global_change_frac` | `0.45` | سهم تغییر «سراسری» (زوم/تعویض) |
| `live.live_edge_frac` / `weight` | `0.10` / `0.25` | کندل جاری |
| `live.min_frame_std` | `5` | فریم سیاه |
| `live.frozen_after_sec` | `90` | فریز در پایش |
| `live.vision_cooldown_sec` | `10` | فاصله‌ی تحلیل تصویری |
| `live.speech_cooldown_sec` | `4` | فاصله‌ی تحلیل با محرک گفتار |
| `live.context_sec` | `75` | پنجره‌ی گفتار همراه فریم |
| `live.vision_image_max_width` | `1400` | عرض تصویر ارسالی به مدل |
| `live.vision_prefilter.enabled` | `false` | پیش‌فیلتر ارزان قبل از مدل اصلی (⚠ ممکن است رویداد از دست بدهد) |
| `live.vision_prefilter.model` | `""` | مدل کوچک/سریع، مثلاً `gpt-4o-mini` |
| `live.region` | `null` | ناحیه‌ی چارت (با `--pick-region`) |
| `validation.auto_publish_confidence` | `0.8` | آستانه‌ی ارسال خودکار |
| `validation.min_confidence` | `0.4` | آستانه‌ی رد |
| `validation.min_management_confidence` | `0.6` | آستانه‌ی دستور مدیریتی |
| `validation.require_sl` | `true` | سیگنال بدون استاپ ارسال نمی‌شود |
| `validation.max_entry_distance_pct` | `3.0` | حداکثر فاصله‌ی ورود از بازار |
| `validation.market_entry_tolerance_pct` | `0.3` | تلورانس ورودِ بازار |
| `validation.min_sl_pips` | `2` | حداقل فاصله‌ی استاپ |
| `validation.rr_min` / `rr_max` | `0.2` / `20` | محدوده‌ی R:R |
| `validation.publish_conditional` | `true` | ارسال سیگنال شرطی با برچسب |
| `validation.review_fallback` | `label` | `label` یا `hold` بدون admin_chat |
| `trades.update_window_min` | `30` | بازه‌ی تکرار/اصلاح سیگنال ورود جدید — از آخرین فعالیت معامله (مدیریت معاملات باز محدودیت زمانی ندارد) |
| `trades.update_entry_pct` | `0.1` | اختلاف ورود = همان سیگنال |
| `trades.draft_ttl_sec` | `120` | عمر پیش‌نویس |
| `trades.review_ttl_sec` | `300` | عمر بازبینی |
| `trades.persist` | `true` | بازیابی معاملات باز پس از ری‌استارت (`session_data/trades.json`) |
| `trades.restore_max_gap_min` | `120` | سقف فاصله‌ی آخرین ذخیره برای بازیابی بعد از کرش |
| `market.sources` | `["mt5","chart"]` | ترتیب منابع قیمت — بدون MT5: `["http","chart"]` |
| `market.cache_sec` | `2` | عمر کش قیمت (کلاینت HTTP پایاست؛ handshake تکراری ندارد) |
| `market.mt5_path` | `""` | مسیر terminal64.exe |
| `market.mt5_max_tick_age_sec` | `300` | تیک کهنه |
| `market.broker_symbols` | `{}` | نگاشت دستی نماد |
| `market.chart_price_max_age_sec` | `120` | عمر قیمت چارت |
| `market.stale_stream_pct` | `1.0` | آستانه‌ی پخش قدیمی |
| `market.closed_policy` | `review` | `review` / `block` / `allow` |
| `market.http.preset` | `""` | پیش‌تنظیم آماده: `gold-api` (رایگان بدون کلید برای فلزات) |
| `market.http` | `{}` | یا منبع دستی: `{url, json_path, symbol_map, headers}` |
| `symbols.aliases` | `{}` | نام‌های خاص کانال |
| `symbols.overrides` | `{}` | تعریف pip_size و … |
| `recording.enabled` | `true` | ضبط جلسه |
| `recording.keep_audio` | `true` | نگه‌داشتن صدا |
| `recording.keep_days` | `14` | جلسه‌های قدیمی‌تر حذف می‌شوند (۰=خاموش) |
| `recording.compress_after_days` | `3` | جلسه‌های قدیمی‌تر به zip فشرده می‌شوند (۰=خاموش) |
| `signal.template` | `""` | قالب دلخواه پیام سیگنال: `{symbol} {side} {order_type} {entry} {sl} {tps} {entries} {timeframe} {confidence} {evidence} {condition} {id} {time}` |
| `dashboard.host` / `port` | `127.0.0.1` / `8765` | داشبورد |
| `logging.level` / `dir` | `INFO` / `logs` | لاگ |

---

## ۲۱. عیب‌یابی

| نشانه | علت / درمان |
|---|---|
| «MTProto تلگرام: timed out» | فیلترشکن خاموش است؛ TUN را روشن کنید یا `network.proxy` را ست کنید. برنامه خودش بی‌نهایت تلاش می‌کند. |
| پیام‌ها در کانال نمی‌آیند | `publisher.dry_run: true` است (طبیعی در حالت آزمایش). |
| «فریم نامعتبر (blank)» | اشتراک صفحه قطع شده یا ناحیه‌ی چارت اشتباه است. |
| هیچ سیگنالی ساخته نمی‌شود | `live.region` را دوباره انتخاب کنید؛ بررسی کنید `logs/assistant.log` تحلیل‌ها را نشان دهد. |
| «قیمت نمودار با بازار فاصله دارد» | قیمت مستقل (MT5/API) فعال است و جلسه ضبط‌شده پخش می‌شود؛ یا اختلاف بروکر (تلورانس طلا ۳ دلار). |
| تأخیر زیاد | بخش ۱۶؛ `vision_image_max_width` و `audio_chunk_sec` را کم کنید. |
| «پنجره‌ی Chrome جابه‌جا شد» | پنجره را برگردانید یا `--pick-region` را دوباره بزنید. |

---

## ۲۲. کارهای باقی‌مانده برای تکمیل پروژه

### الف) لازم قبل از استفاده‌ی واقعی
1. **انتخاب ناحیه‌ی چارت**: `python main.py --pick-region` (پنجره‌ی جلسه باز باشد) و پیش‌نمایش را ببینید.
2. **چت ادمین**: یک کانال/چت خصوصی بسازید، ربات را ادمین کنید، شناسه را در `telegram.admin_chat` بگذارید.
3. **`publisher.dry_run: false`** فقط وقتی خواستید سیگنال واقعی برود.
4. **کلیدهای اختصاصی**: `api_id/api_hash` خودتان از my.telegram.org (مقدار عمومی امن نیست) و چرخش (rotate) کلیدهای
   قبلی که در چت رد و بدل شده‌اند.
5. **مجوز ضبط**: اجازه‌ی برگزارکننده برای ضبط صدا/تصویر و حق بازنشر سیگنال‌ها را بررسی کنید (بخش ۲۳).

### ب) کارهای فنی باقی‌مانده (به ترتیب اولویت)
1. **جلسه‌ی واقعی و کالیبراسیون** — یک جلسه‌ی کامل ضبط کنید، `labels.json` بسازید، `--replay` بزنید و
   `precision` را اندازه بگیرید؛ هدف اولیه ≥ ۰.۹. سپس آستانه‌ها (`auto_publish_confidence`, `change_threshold`,
   `vision_cooldown_sec`) را تنظیم کنید. *(فقط با جلسه‌ی واقعی ممکن است — نیازمند کاربر)*
2. **MT5 در venv 3.12** (دستورهای بخش ۱۵) و بررسی `market_source` در داشبورد: باید `mt5: وصل …` ببینید.
   تا آن موقع می‌توانید با `market.http.preset: "gold-api"` قیمت مستقل HTTP داشته باشید (فلزات).
3. **مدیریت چند گوینده** (diarization ساده با تفکیک جنس صدا) تا وقتی تحلیلگر اصلی ساکت است، حرف مهمان سیگنال نشود
   (فعلاً فقط `NOT_ANALYST` از مدل).
4. **بسته‌بندی exe** با PyInstaller — اجرای خودکار هم‌اکنون با `start_forever.bat` + Task Scheduler انجام می‌شود
   (بخش ۱۰ اسکریپت‌ها)؛ exe فقط ساده‌سازی توزیع است.
5. **هم‌پوشانی صدا در مسیر پشتیبان `sounddevice`** — ✅ انجام شد (۲.۲.۰): `_worker_queue` همپوشانی و
   زمان‌بندی قطعه دارد و تست آفلاین دارد.
6. **تلاش مجدد مدل بینایی با backoff** — ✅ انجام شد (۲.۲.۰) برای STT و هر چهار مسیر مدل، به‌همراه حفظ
   محرک گفتاری پس از خطا.
7. **پیش‌فیلتر ارزان** — ✅ انجام شد (۲.۲.۰) به‌صورت opt-in با `live.vision_prefilter` (پس از کالیبراسیون
   جلسه‌ی واقعی می‌توانید روشن کنید).
8. **بازگشت خودکار به جلسه (reconnect)** — ✅ انجام شد (۲.۲.۰): تا ۳ تلاش با ورود مجدد مرورگر؛ سهمیه پس از
   ۱۰ دقیقه پایداری تازه می‌شود.
9. **`REENTRY` / `ADD_ENTRY` با درصد حجم** — ✅ انجام شد (۲.۲.۰): مدل `portion` را از گفتار («نصف حجم»)
   برمی‌گرداند و در entries/پیام مدیریتی اعمال می‌شود.
10. **تست‌های بیشتر** — ✅ انجام شد (۲.۲.۰): ۱۸ تست جدید در `tests/test_extra.py` (صف outbox، callback،
    MT5/HTTP ماک، همپوشانی صدا، سلامت، قالب، reconnect، چرخش).
11. **بسته‌بندی و اجرای خودکار** — ✅ انجام شد (۲.۲.۰): `start_forever.bat` + `scripts/install_autostart_task.ps1`.
12. **چرخش داده‌ها** — ✅ انجام شد (۲.۲.۰): `recording.keep_days` / `compress_after_days`.
13. **انتخاب دستگاه صدا** — ✅ انجام شد (۲.۲.۰): `capture.audio_device` + داشبورد + `POST /api/audio-device`.
14. **منبع قیمت HTTP جایگزین** — ✅ انجام شد (۲.۲.۰): پیش‌تنظیم `gold-api` (رایگان بدون کلید).
15. **مستندسازی فارسی پیام‌های کانال** — ✅ انجام شد (۲.۲.۰): قالب دلخواه با `signal.template`.
16. **هم‌زمانی و ترتیب خط لوله** — ✅ انجام شد (۲.۳.۰): تبدیل گفتار به‌ترتیب قطعه‌ها (کارگر صف)، سد سکوت بر اساس سطح هر قطعه، تحلیل بینایی غیربلاک‌کننده (رویداد حین تحلیل گم نمی‌شود).
17. **کارایی تصویر و منابع** — ✅ انجام شد (۲.۳.۰): کدگذاری PNG/JPEG در thread، mss ماندگار per-thread، انتخاب مانیتور درست در چند مانیتور، کلاینت HTTP پایا، هرس نگاشت sent و flush بی‌نوشت.

### ج) کارهای حقوقی/عملیاتی
1. بررسی مجوز بازنشر محتوای جلسه و سیگنال‌ها.
2. درج سلب مسئولیت (اکنون در انتهای هر پیام هست).
3. تعیین سیاست نگه‌داری صدا/تصویر (پیش‌فرض: نگه‌داری کامل — در صورت نیاز خاموش کنید).

---

## ۲۳. محدودیت‌ها

- سیگنال‌ها فقط از متن گفتار و تصویر استخراج می‌شوند؛ **هیچ معامله‌ای اجرا نمی‌شود**.
- دقت به کیفیت گفتار (اکو/نویز/لهجه) و خوانایی چارت بستگی دارد.
- مدل بینایی می‌تواند عدد را اشتباه بخواند؛ تور ایمنی جلوی بخشی از آن را می‌گیرد، نه همه را.
- پیپ طلا بین تحلیلگرها مبهم است؛ به همین دلیل تبدیل پیپ طلا هرگز مستقیم ارسال نمی‌شود.
- تشخیص «ضبط قدیمی» فقط با قیمت مستقل ممکن است (MT5/HTTP).
- داشبورد بدون احراز هویت است و فقط روی 127.0.0.1 گوش می‌دهد.

---

## ۲۴. تست‌ها

`tests/test_scenarios.py` (۳۶ تست، بدون API و تلگرام):

| گروه | پوشش |
|---|---|
| symbols | نگاشت نام بروکر/فارسی، تکمیل اعداد خلاصه، ارقام فارسی، «و نیم» |
| lexicon | اصلاح STT، اعداد حرفی، NEGATION/HEDGE/PAST، حذف همپوشانی |
| §1 انواع اعلام | MARKET، LIMIT، ZONE، پله‌ای، پیپ (طلا/یورو)، TP باز، R-multiple، ترتیب TP، CONDITIONAL |
| §3 intent | HYPOTHETICAL / REVIEW_PAST / BIAS_ONLY / NOT_A_SIGNAL هرگز ارسال نمی‌شوند |
| تور ایمنی | نفی، گذشته، فقط تصویر، رسم قدیمی، چارت ناهمخوان، ضبط قدیمی |
| §8 اعتبارسنجی | SL/TP سمت اشتباه، فاصله، نوع سفارش، R:R، آستانه‌ی اطمینان |
| §9 ماشین حالت | ادغام پیش‌نویس، تکرار، اصلاح (Edit)، Reply مدیریت، برگشت پوزیشن، تأیید ادمین |
| فریم | سیاه، محلی، لبه‌ی راست، سراسری |
| بازار | جمعه/شنبه بسته، کریپتو باز |
| منطقه/DPI | تبدیل بوم→فیزیکی، رفت‌وبرگشت ۱.۰/۱.۲۵/۱.۵/۲.۰، اعتبارسنجی بیرون صفحه، نوشتن در YAML |
| تأخیر | ریاضی مراحل، تکمیل مرحله‌ی تلگرام، میانگین/بیشینه، تشخیص گلوگاه |

`tests/test_extra.py` (۴۱ تست تکمیلی، ۲.۲.۰ + ۲.۳.۰):

| گروه | پوشش |
|---|---|
| صدا | همپوشانی و حفظ نمونه‌ها در مسیر sounddevice، زمان‌بندی قطعه (`chunk_times`) و فایل WAV |
| صف outbox | ترتیب sendMessage/Reply/Edit، کهنه‌شدن + گزارش ادمین، ماندگاری صف بین اجراها |
| callback ادمین | تأیید → SENT و پیام کانال، رد → REJECTED، شناسه‌ی ناشناخته |
| بازار | MT5 ماک: پیدا کردن نماد بروکر، قیمت، تیک کهنه؛ پیش‌تنظیم gold-api و ادغام symbol_map |
| سلامت | پنجره‌ی ماک: missing/minimized/moved + تلورانس؛ فریم سیاه/فریز با آستانه |
| قالب پیام | `signal.template` رندر متغیرها + fallback پیش‌فرض |
| پله‌ی حجم | REENTRY با درصد حجم → entries؛ ADD_ENTRY با portion → پیام «۳۰٪ حجم» |
| پیش‌فیلتر | رد فریم بی‌رویداد بدون مدل اصلی؛ عبور محرک گفتاری همیشه |
| reconnect | تلاش‌ها و سهمیه، غیرفعال‌بودن، نیاز به لینک جلسه |
| چرخش داده | حذف/zip جلسات قدیمی بر اساس keep_days/compress_after_days |
| ترتیب STT (۲.۳.۰) | قطعه‌ی کُند اول → ترتیب تبدیل همچنان به‌ترتیب تولید؛ ادغام همپوشانی سالم |
| سد سکوت (۲.۳.۰) | قطعه‌ی پرصدا بعد از قطعه‌ی بی‌صدا حذف نمی‌شود (سطح هر قطعه ملاک است) |
| مانیتور (۲.۳.۰) | انتخاب مانیتور حاوی ناحیه + کلیپ ناحیه‌ی نیمه‌بیرون‌زده |
| تحلیل غیربلاک‌کننده (۲.۳.۰) | رویداد چارت حین تحلیل قبلی pending می‌ماند و بعد اجرا می‌شود |
| کلاینت HTTP (۲.۳.۰) | پایا‌بودن کلاینت قیمت + ساخت تازه پس از aclose |
| ترتیب انتشار (۲.۳.۰) | دو سیگنال پشت‌سرهم (با pending حین تحلیل) → ترتیب T1/T2 در کانال حفظ می‌شود |
| انقضاها (۲.۳.۰) | پیش‌نویس پس از draft_ttl_sec منقضی و گزارش ادمین؛ پس از ۳۰ دقیقه بی‌فعالیتی (از آخرین فعالیت) سیگنال همان تکراری نیست، معامله‌ی جدید است |
| مدیریت هر-سن (۲.۳.۰) | Reply مدیریتی به معامله‌ی ۹۰ دقیقه‌ای وصل می‌شود؛ تکرار بر اساس آخرین فعالیت گرفته می‌شود |
| پایان جلسه (۲.۳.۰) | سکوت ۸ دقیقه → پایان خودکار؛ تحلیل نیمه‌کاره حین پایان قطع می‌شود و هیچ سیگنال کانالی بعد از پایان ساخته نمی‌شود |
| رویداد در cooldown (۲.۳.۰) | رویداد چارت حین cooldown حذف نمی‌شود؛ پس از آن یک تحلیل با تازه‌ترین فریم؛ رویدادهای یک بازه ادغام می‌شوند |
| هرس ایمن sent_map (۲.۳.۰) | نگاشت پیام معامله‌های زنده (SENT/REVIEW) مستثنا از هرس ۲۴ ساعته؛ بسته‌شده‌ها هرس می‌شوند |
| پایداری معاملات (۲.۳.۰) | trades.json اتمیک؛ کرش → همه‌ی زنده‌ها بازیابی و نگاشتشان محافظت می‌شود؛ پایان تمیز جلسه → بازیابی نمی‌شود؛ شماره‌ها ادامه می‌یابند |
| نوشتن اتمیک (۲.۳.۰) | قفل موقت فایل → چند تلاش مجدد؛ قفل پایدار → فقط هشدار، بدون کرش |
| گله‌ی کهنگی (۲.۳.۰) | saved_at قدیمی‌تر از restore_max_gap_min → بدون بازیابی؛ مدیریت به معامله‌ی قدیمی وصل نمی‌شود؛ شماره‌ها ادامه می‌یابند |
| ذخیره‌ی دوره‌ای (۲.۳.۰) | جلسه‌ی ۱۵۰ دقیقه‌ای بی‌تغییر → tick هر ~۱۵ ثانیه saved_at را تازه می‌کند و ری‌استارت فوری بازیابی می‌کند؛ پرچم پایان با ذخیره‌ی عقب‌افتاده خنثی نمی‌شود؛ load فایل کهنه را تازه نمی‌کند |
| آمار پایان جلسه (۲.۳.۰) | شمارش تحلیل بینایی/محرک/رویداد/خطا در stats و meta پایان جلسه |
| صف outbox (۲.۳.۰) | flush خالی فایل نمی‌نویسد؛ هرس نگاشت sent پس از ۲۴ ساعت |

اجرا:
```powershell
$env:PYTHONUTF8='1'
python tests/test_scenarios.py
python tests/test_extra.py
python -m app.test_live
```
