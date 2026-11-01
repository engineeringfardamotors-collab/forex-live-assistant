# دستیار جلسات لایو فارکس

ورود خودکار به جلسه‌ی آنلاین تحلیلگر (لینک از گروه تلگرام) → ضبط صدا و چارت → استخراج سیگنال با مدل بینایی
→ اعتبارسنجی → ارسال به کانال تلگرام → مدیریت پس از ورود (Reply/Edit).

نسخه **2.3.0** · مستندات کامل: [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md) · تغییرات: [`CHANGELOG.md`](CHANGELOG.md)

## اجرای سریع

```powershell
$env:PYTHONUTF8='1'
python -m pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env      # و رازها را پر کنید
python main.py --send-code  # ورود یک‌باره‌ی تلگرام
python main.py --sign-in 12345
python main.py --check-net
python main.py --pick-region     # انتخاب ناحیه‌ی چارت با ماوس
python main.py                   # اجرا + داشبورد http://127.0.0.1:8765
```

`publisher.dry_run` پیش‌فرض `true` است: **هیچ پیامی به کانال نمی‌رود** تا خودتان `false` کنید.

## اجرای دائمی (اختیاری)

```powershell
.\start_forever.bat                                  # ناظر: اگر برنامه کرش کرد دوباره اجرا می‌شود
powershell -ExecutionPolicy Bypass -File scripts\install_autostart_task.ps1   # شروع خودکار با ورود به ویندوز
```

## وضعیت فعلی (مهم)

- قیمت مستقل MT5 فعال نیست (ویل `MetaTrader5` برای Python 3.15 وجود ندارد). راه‌حل ۱: محیط مجازی 3.12 —
  دستورها در بخش ۱۵ مستندات. راه‌حل ۲: منبع HTTP رایگان با `market.http.preset: "gold-api"` (بخش ۲۰).
- `telegram.admin_chat` خالی است؛ سیگنال‌های مشکوک با برچسب «⚠️ نیاز به تأیید» در کانال می‌روند.
  برای بازبینی قبل از انتشار، یک چت ادمین تنظیم کنید.
- قطعی جلسه خودکار دوباره وصل می‌شود (`meeting.reconnect`)؛ اگر جلسه واقعاً تمام شده باشد، پس از تلاش‌ها پایان می‌یابد.

## تست

```powershell
python tests/test_scenarios.py   # ۳۶ تست آفلاین
python tests/test_extra.py       # ۴۱ تست تکمیلی (صف ارسال، MT5 ماک، صدا، سلامت، ترتیب STT/انتشار، ...)
python -m app.test_live          # تست واقعی مدل بینایی
```

## فهرست کارهای باقی‌مانده

بخش ۲۲ مستندات: `docs/DOCUMENTATION.md`.
