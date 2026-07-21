"""Shared paths, defaults, and environment helpers."""

from __future__ import annotations

import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

# Lovense Standard Socket API
DEFAULT_TOKEN_URL = "https://api.lovense-api.com/api/basicApi/getToken"
DEFAULT_SOCKET_URL_API = "https://api.lovense-api.com/api/basicApi/getSocketUrl"
DEFAULT_PLATFORM = "Rys Circus"

# Legacy HTTP endpoints (pairing fallback not used; kept for reference)
DEFAULT_QR_URL = "https://api.lovense-api.com/api/lan/getQrCode"

DEFAULT_ACTION = "Vibrate:16"
DEFAULT_TIME_SEC = 5.0
DEFAULT_COOLDOWN = 3.0
DEFAULT_PHRASE = "to infinity and beyond"
DEFAULT_TRIGGERS_FILE = SCRIPT_DIR / "triggers.json"
DEFAULT_UNAME = "BuzzLightyear"
DEFAULT_UTOKEN_SALT = "buzzlightyear"


def env(name: str, default: str | None = None) -> str | None:
    """Return a stripped env var, or default if missing/blank."""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def upsert_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file, replacing an existing assignment if present."""
    line = f"{key}={value}"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        key_prefix = f"{key}="
        replaced = False
        out: list[str] = []
        for existing in lines:
            stripped = existing.strip()
            if stripped.startswith(key_prefix) or stripped.startswith(f"# {key_prefix}"):
                if not replaced:
                    out.append(line)
                    replaced = True
                continue
            out.append(existing)
        if not replaced:
            if out and out[-1].strip() != "":
                out.append("")
            out.append(line)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    else:
        path.write_text(
            "# Auto-created by voice_trigger.py --pair\n"
            f"{line}\n",
            encoding="utf-8",
        )
