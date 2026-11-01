"""انتخاب ناحیه‌ی چارت با ماوس:  python main.py --pick-region

مختصات ذخیره‌شده «پیکسل فیزیکی» هستند؛ همان چیزی که mss هنگام ضبط می‌گیرد.
تبدیل‌ها:
  - کلیک روی پیش‌نمایش کوچک‌شده  ─(× نسبت تصویر/بوم)─►  پیکسل تصویر mss
  - پیکسل تصویر                   ─(+ مبدأ مانیتور)──►    مختصات صفحه‌ی مجازی
چون نسبت از اندازه‌ی واقعی تصویر و بوم محاسبه می‌شود، نتیجه به مقیاس ویندوز
(100٪/125٪/150٪) و DPI-aware بودن tkinter وابسته نیست.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app import winapi

MIN_W, MIN_H = 200, 120


class RegionError(ValueError):
    pass


# ---------------- pure helpers (unit-tested) ----------------
def normalize_region(r: dict[str, Any] | None) -> dict[str, int] | None:
    """پذیرش {x,y,width,height} یا {left,top,width,height} → {left,top,width,height}."""
    if not r:
        return None
    left = r.get("left", r.get("x"))
    top = r.get("top", r.get("y"))
    if left is None or top is None or r.get("width") is None or r.get("height") is None:
        return None
    return {"left": int(left), "top": int(top), "width": int(r["width"]), "height": int(r["height"])}


def logical_to_physical(rect: tuple[float, float, float, float], scale: float) -> tuple[int, int, int, int]:
    """(x, y, w, h) منطقی (DPI-unaware) → فیزیکی. برای مقیاس 1.25: 100 → 125."""
    x, y, w, h = rect
    return round(x * scale), round(y * scale), round(w * scale), round(h * scale)


def physical_to_logical(rect: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    x, y, w, h = rect
    return round(x / scale), round(y / scale), round(w / scale), round(h / scale)


def selection_to_region(sel: tuple[float, float, float, float], canvas_size: tuple[int, int],
                        image_size: tuple[int, int], monitor: dict[str, int]) -> dict[str, int]:
    """مستطیل انتخاب‌شده روی بوم (x0,y0,x1,y1 در هر جهتی) → ناحیه‌ی فیزیکی روی صفحه."""
    x0, y0, x1, y1 = sel
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    cw, ch = canvas_size
    iw, ih = image_size
    sx, sy = iw / cw, ih / ch
    left = max(0, min(iw, round(x0 * sx)))
    top = max(0, min(ih, round(y0 * sy)))
    right = max(0, min(iw, round(x1 * sx)))
    bottom = max(0, min(ih, round(y1 * sy)))
    return {"left": monitor["left"] + left, "top": monitor["top"] + top,
            "width": right - left, "height": bottom - top}


def validate_region(region: dict[str, int] | None, monitors: list[dict[str, int]]) -> dict[str, int]:
    """ناحیه باید کامل داخل یکی از مانیتورها باشد. خروجی: مانیتور دربرگیرنده. وگرنه RegionError."""
    r = normalize_region(region)
    if r is None:
        raise RegionError("ناحیه خالی یا ناقص است (left/top/width/height لازم است).")
    if r["width"] < MIN_W or r["height"] < MIN_H:
        raise RegionError(f"ناحیه خیلی کوچک است ({r['width']}×{r['height']}). حداقل {MIN_W}×{MIN_H} پیکسل.")
    for m in monitors:
        if (r["left"] >= m["left"] and r["top"] >= m["top"]
                and r["left"] + r["width"] <= m["left"] + m["width"]
                and r["top"] + r["height"] <= m["top"] + m["height"]):
            return m
    desc = ", ".join(f"[{m['left']},{m['top']} {m['width']}×{m['height']}]" for m in monitors)
    raise RegionError(
        f"ناحیه‌ی ({r['left']},{r['top']} {r['width']}×{r['height']}) خارج از صفحه است یا بین دو مانیتور افتاده. "
        f"مانیتورها: {desc}. اگر رزولوشن یا مقیاس ویندوز را عوض کرده‌اید، دوباره --pick-region بزنید."
    )


def write_region_to_config(config_path: Path, region: dict[str, int], meta: dict[str, Any]) -> None:
    """فقط خطوط region و region_meta زیر live: را عوض می‌کند تا توضیحات فایل حفظ شود."""
    text = config_path.read_text(encoding="utf-8")
    reg = (f"  region: {{left: {region['left']}, top: {region['top']}, "
           f"width: {region['width']}, height: {region['height']}}}   # پیکسل فیزیکی؛ با --pick-region")
    meta_items = ", ".join(f"{k}: {v}" for k, v in meta.items())
    meta_line = f"  region_meta: {{{meta_items}}}"
    lines = text.splitlines()
    try:
        live_idx = next(i for i, ln in enumerate(lines) if re.match(r"^live:\s*(#.*)?$", ln))
    except StopIteration:
        lines += ["", "live:", reg, meta_line]
    else:
        end = next((i for i in range(live_idx + 1, len(lines)) if re.match(r"^\S", lines[i])), len(lines))
        block = lines[live_idx + 1:end]
        block = [ln for ln in block if not re.match(r"^\s+region_meta:", ln)]
        idx = next((i for i, ln in enumerate(block) if re.match(r"^\s+region:", ln)), None)
        if idx is None:
            block.append(reg)
            idx = len(block) - 1
        else:
            block[idx] = reg
        block.insert(idx + 1, meta_line)
        while block and not block[-1].strip():
            block.pop()
        lines = lines[:live_idx + 1] + block + ([""] if end < len(lines) else []) + lines[end:]
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------- capture ----------------
def _monitors() -> list[dict[str, int]]:
    import mss

    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors[1:]]


def grab(area: dict[str, int]):
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab({k: area[k] for k in ("left", "top", "width", "height")})
        return Image.frombytes("RGB", shot.size, shot.rgb)


def monitor_for_browser(monitors: list[dict[str, int]]) -> tuple[dict[str, int], winapi.WindowInfo | None]:
    win = winapi.find_browser_window()
    if win and not win.minimized:
        cx, cy = (win.rect[0] + win.rect[2]) / 2, (win.rect[1] + win.rect[3]) / 2
        for m in monitors:
            if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                return m, win
    return monitors[0], win


def save_preview(region: dict[str, int], out_dir: Path, full_img=None, monitor: dict[str, int] | None = None) -> Path:
    from PIL import ImageDraw

    out_dir.mkdir(parents=True, exist_ok=True)
    crop = grab(region)
    path = out_dir / "region_preview.png"
    crop.save(path)
    if full_img is not None and monitor is not None:
        img = full_img.copy()
        d = ImageDraw.Draw(img)
        x0, y0 = region["left"] - monitor["left"], region["top"] - monitor["top"]
        for k in range(4):
            d.rectangle([x0 - k, y0 - k, x0 + region["width"] + k, y0 + region["height"] + k], outline=(255, 50, 50))
        img.save(out_dir / "region_preview_full.png")
    return path


# ---------------- interactive ----------------
def _interactive_select(img, title: str) -> tuple[tuple[float, float, float, float], tuple[int, int]] | None:
    import tkinter as tk

    from PIL import ImageTk

    root = tk.Tk()
    root.title(title)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    iw, ih = img.size
    fit = min((sw * 0.94) / iw, (sh * 0.86) / ih, 1.0)
    cw, ch = max(1, int(iw * fit)), max(1, int(ih * fit))
    shown = img.resize((cw, ch))
    photo = ImageTk.PhotoImage(shown)
    tk.Label(root, text="روی چارت بکشید (فقط ناحیه‌ی نمودار، بدون وب‌کم و منوها) • Enter = ذخیره • Esc = لغو • "
                        "کشیدن دوباره = انتخاب جدید", font=("Tahoma", 11)).pack(fill="x")
    canvas = tk.Canvas(root, width=cw, height=ch, cursor="cross", highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    state: dict[str, Any] = {"start": None, "rect": None, "sel": None, "ok": False}
    info = canvas.create_text(10, 10, anchor="nw", fill="#ffef5a", font=("Consolas", 12, "bold"), text="")

    def down(e):  # noqa: ANN001
        state["start"] = (e.x, e.y)
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3355", width=2)

    def move(e):  # noqa: ANN001
        if state["start"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)
            pw, ph = abs(e.x - x0) * iw / cw, abs(e.y - y0) * ih / ch
            canvas.itemconfigure(info, text=f"{pw:.0f} × {ph:.0f} px")

    def up(e):  # noqa: ANN001
        if state["start"]:
            x0, y0 = state["start"]
            state["sel"] = (x0, y0, max(0, min(cw, e.x)), max(0, min(ch, e.y)))

    def accept(_e=None):  # noqa: ANN001
        if state["sel"]:
            state["ok"] = True
            root.destroy()

    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", up)
    root.bind("<Return>", accept)
    root.bind("<Escape>", lambda _e: root.destroy())
    tk.Button(root, text="ذخیره (Enter)", command=accept).pack(pady=4)
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()
    if not state["ok"]:
        return None
    # اندازه‌ی واقعی بوم = اندازه‌ی تصویر نمایش‌داده‌شده (cw, ch)
    return state["sel"], (cw, ch)


def pick_region(config_path: Path, out_dir: Path, rect: str | None = None, monitor_index: int | None = None) -> int:
    aware = winapi.set_dpi_aware()
    monitors = _monitors()
    if monitor_index:
        if not 1 <= monitor_index <= len(monitors):
            print(f"❌ مانیتور {monitor_index} وجود ندارد (1..{len(monitors)})")
            return 2
        mon, win = monitors[monitor_index - 1], winapi.find_browser_window()
    else:
        mon, win = monitor_for_browser(monitors)
    scale = winapi.monitor_scale_at(mon["left"] + 10, mon["top"] + 10)
    print(f"مانیتور: {mon['width']}×{mon['height']} در ({mon['left']},{mon['top']}) • مقیاس ویندوز: {scale:.0%} • "
          f"DPI-aware: {'بله' if aware else 'خیر'}")
    if win:
        print(f"پنجره‌ی مرورگر: «{win.title[:60]}» {win.width}×{win.height}"
              + (" (کوچک‌شده!)" if win.minimized else ""))
    else:
        print("⚠️ پنجره‌ی Chrome/Edge پیدا نشد؛ از مانیتور اصلی عکس گرفته می‌شود.")
    full = grab(mon)
    if full.size != (mon["width"], mon["height"]):
        print(f"⚠️ اندازه‌ی عکس {full.size} با مانیتور فرق دارد؛ نسبت از روی خود عکس محاسبه می‌شود.")

    if rect:
        try:
            x, y, w, h = (int(float(v)) for v in rect.split(","))
        except ValueError:
            print("❌ قالب --region-rect باید x,y,width,height باشد")
            return 2
        region = {"left": x, "top": y, "width": w, "height": h}
    else:
        res = _interactive_select(full, "انتخاب ناحیه‌ی چارت")
        if res is None:
            print("لغو شد؛ چیزی ذخیره نشد.")
            return 1
        sel, canvas_size = res
        region = selection_to_region(sel, canvas_size, full.size, mon)

    try:
        validate_region(region, monitors)
    except RegionError as e:
        print(f"❌ {e}")
        return 2
    meta: dict[str, Any] = {"screen_w": mon["width"], "screen_h": mon["height"], "scale": round(scale, 2)}
    if win and not win.minimized:
        meta.update({"win_left": win.rect[0], "win_top": win.rect[1], "win_w": win.width, "win_h": win.height})
    write_region_to_config(config_path, region, meta)
    preview = save_preview(region, out_dir, full, mon)
    print(f"✅ ذخیره شد در {config_path.name}: left={region['left']} top={region['top']} "
          f"width={region['width']} height={region['height']}")
    print(f"پیش‌نمایش: {preview.resolve()}")
    print(f"           {(out_dir / 'region_preview_full.png').resolve()}")
    print("نکته: بعد از این، پنجره‌ی Chrome را جابه‌جا/تغییر اندازه ندهید؛ اگر دادید دوباره --pick-region بزنید.")
    return 0
