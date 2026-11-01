@echo off
REM راه‌انداز دستیار جلسات لایو فارکس
REM برای اجرای خودکار در زمان روشن‌شدن ویندوز از Task Scheduler استفاده کنید (راهنما: docs/DOCUMENTATION.md)
set PYTHONUTF8=1
cd /d "%~dp0"
if exist ".venv312\Scripts\python.exe" (
  echo [i] اجرا با پایتون 3.12 و MetaTrader5
  ".venv312\Scripts\python.exe" main.py %*
) else (
  python main.py %*
)
echo.
echo برنامه بسته شد. برای اجرای دوباره این فایل را بزنید.
pause
