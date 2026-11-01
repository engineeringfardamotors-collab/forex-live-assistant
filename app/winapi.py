"""توابع کمکی ویندوز با ctypes: DPI، پنجره‌ی کروم، قفل صفحه، جلوگیری از خواب، رویداد Suspend.

همه‌ی مختصات در «پیکسل فیزیکی» هستند؛ همان دستگاه مختصاتی که mss با آن عکس می‌گیرد.
برای این کار پروسه باید Per-Monitor DPI Aware باشد (set_dpi_aware) — در غیر این صورت
ویندوز در مقیاس 125٪/150٪ مختصات «منطقی» کوچک‌شده برمی‌گرداند.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

IS_WIN = sys.platform == "win32"


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]   # left, top, right, bottom (فیزیکی)
    minimized: bool
    maximized: bool
    pid: int

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


_dpi_state: dict[str, bool] = {}


def set_dpi_aware() -> bool:
    """پروسه را Per-Monitor DPI Aware می‌کند. True اگر مختصات فیزیکی‌اند."""
    if not IS_WIN:
        return True
    if "aware" in _dpi_state:
        return _dpi_state["aware"]
    aware = False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        aware = bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:  # noqa: BLE001
        pass
    if not aware:
        try:
            hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
            aware = hr in (0, -2147024891)  # S_OK یا E_ACCESSDENIED (قبلاً تنظیم شده)
        except Exception:  # noqa: BLE001
            pass
    if not aware:
        try:
            # اگر قبلاً (مثلاً توسط mss) تنظیم شده باشد
            ctx = ctypes.windll.user32.GetThreadDpiAwarenessContext()
            aware = ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(ctx) in (1, 2)
        except Exception:  # noqa: BLE001
            pass
    _dpi_state["aware"] = aware
    return aware


def dpi_scale(hwnd: int | None = None) -> float:
    """ضریب مقیاس ویندوز (1.0، 1.25، 1.5، ...) برای پنجره یا کل سیستم."""
    if not IS_WIN:
        return 1.0
    try:
        u = ctypes.windll.user32
        dpi = u.GetDpiForWindow(hwnd) if hwnd else u.GetDpiForSystem()
        return (dpi or 96) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def monitor_scale_at(x: int, y: int) -> float:
    """مقیاس DPI مانیتوری که نقطه‌ی (x, y) فیزیکی روی آن است."""
    if not IS_WIN:
        return 1.0
    try:
        mon = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return dx.value / 96.0
    except Exception:  # noqa: BLE001
        pass
    return dpi_scale()


# ---------------- windows ----------------
def _window_info(hwnd: int) -> WindowInfo | None:
    u = ctypes.windll.user32
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    r = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS=9: کادر واقعی بدون سایه‌ی نامرئی
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        u.GetWindowRect(hwnd, ctypes.byref(r))
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(hwnd, buf.value, (r.left, r.top, r.right, r.bottom),
                      bool(u.IsIconic(hwnd)), bool(u.IsZoomed(hwnd)), pid.value)


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_browser_windows() -> list[WindowInfo]:
    """پنجره‌های سطح‌بالای قابل‌مشاهده‌ی Chrome/Edge (کلاس Chrome_WidgetWin_1 با عنوان)."""
    if not IS_WIN:
        return []
    out: list[WindowInfo] = []
    u = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):  # noqa: ANN001
        try:
            if u.IsWindowVisible(hwnd) and _class_name(hwnd) == "Chrome_WidgetWin_1":
                info = _window_info(hwnd)
                if info and info.title and (info.width > 200 or info.minimized):
                    out.append(info)
        except Exception:  # noqa: BLE001
            pass
        return True

    u.EnumWindows(proto(cb), 0)
    return out


def find_browser_window(title_hint: str | None = None) -> WindowInfo | None:
    wins = list_browser_windows()
    if not wins:
        return None
    if title_hint:
        hint = title_hint.strip().lower()[:40]
        match = [w for w in wins if hint and hint in w.title.lower()]
        if match:
            return match[0]
    keys = ("meet", "zoom", "teams", "webex", "skyroom", "اسکای", "جلسه", "adobe connect")
    match = [w for w in wins if any(k in w.title.lower() for k in keys)]
    if match:
        return match[0]
    return max(wins, key=lambda w: w.width * w.height)


def foreground_window() -> WindowInfo | None:
    if not IS_WIN:
        return None
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return _window_info(hwnd) if hwnd else None


def process_name(pid: int) -> str:
    if not IS_WIN or not pid:
        return ""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        k.CloseHandle(h)
    return ""


# ---------------- lock / sleep ----------------
def screen_locked() -> bool:
    """قفل بودن صفحه (Win+L) یا نمایش صفحه‌ی امن (UAC)."""
    if not IS_WIN:
        return False
    u = ctypes.windll.user32
    try:
        desk = u.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desk:
            return True
        ok = u.SwitchDesktop(desk)
        u.CloseDesktop(desk)
        if not ok:
            return True
    except Exception:  # noqa: BLE001
        return False
    fg = foreground_window()
    return bool(fg and process_name(fg.pid) == "lockapp.exe")


ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def keep_awake(on: bool) -> bool:
    """در طول جلسه از خواب رفتن سیستم و خاموش شدن نمایشگر جلوگیری می‌کند (مثل پخش ویدیو)."""
    if not IS_WIN:
        return True
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


def idle_seconds() -> float:
    if not IS_WIN:
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0


class PowerEvents:
    """پنجره‌ی پنهان برای دریافت WM_POWERBROADCAST (PBT_APMSUSPEND / RESUME)."""

    WM_POWERBROADCAST = 0x0218
    PBT_APMSUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND = 0x4, 0x12, 0x7

    def __init__(self, callback: Callable[[str], None]):
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._wndproc = None

    def start(self) -> None:
        if IS_WIN and self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="power-events")
            self._thread.start()

    def stop(self) -> None:
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        try:
            u = ctypes.windll.user32
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            u.DefWindowProcW.restype = LRESULT
            u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            def proc(hwnd, msg, wp, lp):  # noqa: ANN001
                if msg == self.WM_POWERBROADCAST:
                    if wp == self.PBT_APMSUSPEND:
                        self.callback("suspend")
                    elif wp in (self.PBT_APMRESUMEAUTOMATIC, self.PBT_APMRESUMESUSPEND):
                        self.callback("resume")
                    return 1
                if msg == 0x0010:  # WM_CLOSE
                    u.DestroyWindow(hwnd)
                    u.PostQuitMessage(0)
                    return 0
                return u.DefWindowProcW(hwnd, msg, wp, lp)

            self._wndproc = WNDPROC(proc)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "FLA_PowerEvents"
            u.RegisterClassW(ctypes.byref(wc))
            u.CreateWindowExW.restype = wintypes.HWND
            # پنجره‌ی سطح‌بالای نامرئی (نه message-only؛ آن‌ها broadcast نمی‌گیرند)
            self._hwnd = u.CreateWindowExW(0, "FLA_PowerEvents", "FLA", 0, 0, 0, 0, 0, None, None, hinst, None)
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001
            pass
