@echo off
REM ناظر اجرای دستیار جلسات لایو فارکس — اگر برنامه کرش کند، خودکار دوباره اجرا می‌شود.
REM برای اجرای خودکار در زمان روشن‌شدن ویندوز:
REM   scripts\install_autostart_task.ps1  (Task Scheduler با همین فایل)
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"

set PY=python
if exist ".venv312\Scripts\python.exe" set PY=.venv312\Scripts\python.exe
echo [i] اجرای پایدار با: %PY%
echo [i] برای توقف، این پنجره را ببندید (Ctrl+C).

:loop
"%PY%" main.py %*
echo.
echo [%date% %time%] برنامه بسته شد؛ اجرای دوباره در ۱۰ ثانیه‌ی دیگر... (برای خروج این پنجره را ببندید)
timeout /t 10 /nobreak >nul
goto loop
