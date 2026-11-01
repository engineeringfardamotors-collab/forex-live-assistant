"""پیکربندی و بارگذاری تنظیمات پروژه."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001
    pass


DEFAULTS: dict[str, Any] = {
    "telegram": {},
    "meeting": {"use_browser": True, "browser_debug_port": 9222},
    "capture": {
        "screenshot_interval_sec": 30,
        "max_frames_per_analysis": 6,
        "audio_sample_rate": 16000,
        "audio_chunk_sec": 20,
    },
    "analysis": {"analysis_interval_min": 5, "min_transcript_chars": 200},
    "signal": {"min_signal_fields": 3, "send_opportunity_alerts": True},
    "publisher": {"dry_run": False, "delay_note_sec": 60, "drop_after_sec": 3600, "retry_sec": 5},
    "health": {"check_interval_sec": 5, "black_after_sec": 30, "frozen_after_sec": 30,
               "move_tolerance_px": 8, "prevent_sleep": True},
    "logging": {"level": "INFO", "dir": "logs"},
}

_config: dict[str, Any] | None = None

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """بارگذاری config.yaml؛ اگر نبود، از config.example.yaml و متغیرهای محیطی."""
    global _config
    root = Path(__file__).resolve().parent.parent
    cfg_path = Path(path) if path else root / "config.yaml"
    if not cfg_path.exists():
        cfg_path = root / "config.example.yaml"
    data: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULTS, data)

    # متغیرهای محیطی اولویت دارند (برای اجرا در محیط‌های مختلف)
    tg = cfg["telegram"]
    tg["bot_token"] = os.getenv("BOT_TOKEN", tg.get("bot_token", ""))
    tg["signal_channel"] = os.getenv("SIGNAL_CHANNEL", tg.get("signal_channel", ""))
    tg["api_id"] = int(
        os.getenv("TELEGRAM_API_ID", os.getenv("TG_API_ID", tg.get("api_id") or 0)) or 0
    )
    tg["api_hash"] = os.getenv("TELEGRAM_API_HASH", os.getenv("TG_API_HASH", tg.get("api_hash", "")))
    tg["phone"] = os.getenv("TELEGRAM_PHONE", tg.get("phone", ""))
    tg["watch_groups"] = [
        g.strip()
        for g in os.getenv("TG_WATCH_GROUPS", ",".join(tg.get("watch_groups", []))).split(",")
        if g.strip()
    ]
    sinox = cfg.get("ai", {}).get("sinox", {})
    sinox["api_key"] = os.getenv("SINOX_API_KEY", sinox.get("api_key", ""))
    avalai = cfg.get("ai", {}).get("avalai", {})
    avalai["api_key"] = os.getenv("AVALAI_API_KEY", avalai.get("api_key", ""))
    openai = cfg.get("ai", {}).get("openai", {})
    openai["api_key"] = os.getenv("OPENAI_API_KEY", openai.get("api_key", ""))

    _config = cfg
    return cfg


def get_config() -> dict[str, Any]:
    if _config is None:
        return load_config()
    return _config
