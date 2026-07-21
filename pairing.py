"""
Pairing session manager for the web application.

The Lovense device owner visits the hosted web UI, clicks Pair, scans a QR
with Lovense Connect / Remote, and this module tracks when authentication
completes. Developer token stays on the server only.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from actions import LovenseError, LovenseSocketClient, generate_uid
from config import (
    DEFAULT_PLATFORM,
    DEFAULT_SOCKET_URL_API,
    DEFAULT_TOKEN_URL,
    DEFAULT_UNAME,
    ENV_PATH,
    SCRIPT_DIR,
    env,
    upsert_env_value,
)

def pairing_state_path() -> Path:
    """JSON file for last pair result (override with PAIRING_STATE_PATH for Docker volumes)."""
    override = env("PAIRING_STATE_PATH")
    if override:
        return Path(override)
    return SCRIPT_DIR / "pairing_state.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_qr_fields(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("data") if isinstance(response.get("data"), dict) else response
    if not isinstance(body, dict):
        body = {}
    return {
        "qrcode_url": body.get("qrcodeUrl")
        or body.get("qr")
        or body.get("qrcode_url"),
        "qrcode": body.get("qrcode"),
        "code": body.get("code") or body.get("deviceCode"),
        "raw": response,
    }


@dataclass
class PairingSession:
    """One owner's in-progress or completed pairing on the server."""

    uid: str
    uname: str
    platform: str
    client: LovenseSocketClient
    started_at: str = field(default_factory=_utc_now)
    qrcode_url: str | None = None
    qrcode: str | None = None
    pair_code: str | None = None
    status: str = "connecting"  # connecting | waiting_for_scan | paired | error
    message: str = ""
    paired_at: str | None = None
    error: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def public_dict(self) -> dict[str, Any]:
        with self._lock:
            device = self.client.last_device_info
            app_status = self.client.last_app_status
            app_online = self.client.last_app_online
            toys = None
            online = None
            if isinstance(device, dict):
                online = device.get("online")
                toys = device.get("toyList") or device.get("toys")
            return {
                "uid": self.uid,
                "uname": self.uname,
                "platform": self.platform,
                "status": self.status,
                "message": self.message,
                "qrcode_url": self.qrcode_url,
                "qrcode": self.qrcode,
                "pair_code": self.pair_code,
                "paired_at": self.paired_at,
                "error": self.error,
                "started_at": self.started_at,
                "device_online": online,
                "toys": toys,
                "app_status": app_status,
                "app_online": app_online,
                "paired": self.status == "paired",
            }

    def refresh_paired_state(self) -> None:
        """Mark paired when Lovense reports the app/device online after scan."""
        with self._lock:
            if self.status == "paired":
                return
            if self.client.is_user_paired():
                self.status = "paired"
                self.paired_at = self.paired_at or _utc_now()
                self.message = "Paired — Lovense Connect is linked."
                _persist_state(self)
                upsert_env_value(ENV_PATH, "LOVENSE_UID", self.uid)
                upsert_env_value(ENV_PATH, "LOVENSE_UNAME", self.uname)
                upsert_env_value(ENV_PATH, "LOVENSE_PLATFORM", self.platform)


class PairingManager:
    """
    Server-side pairing orchestrator (singleton for a single-owner deployment).

    Flow:
      start() → Socket auth + QR → status waiting_for_scan
      poll status() → detects scan via socket events → paired
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session: PairingSession | None = None

    @property
    def session(self) -> PairingSession | None:
        with self._lock:
            return self._session

    def start(
        self,
        *,
        uname: str | None = None,
        uid: str | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        """
        Begin pairing for the device owner.

        Returns a public status dict including qrcode_url when ready.
        """
        token = env("LOVENSE_TOKEN")
        if not token:
            raise LovenseError(
                "Server missing LOVENSE_TOKEN. Set it in the server .env file."
            )

        platform = env("LOVENSE_PLATFORM", DEFAULT_PLATFORM) or DEFAULT_PLATFORM
        uname = (uname or env("LOVENSE_UNAME", DEFAULT_UNAME) or DEFAULT_UNAME).strip()
        uid = (uid or env("LOVENSE_UID") or "").strip() or generate_uid()

        with self._lock:
            if self._session is not None and not force_new:
                existing = self._session
                if existing.status in ("waiting_for_scan", "paired", "connecting"):
                    existing.refresh_paired_state()
                    # Refresh QR if still waiting and we already have one
                    if existing.status == "waiting_for_scan" and existing.qrcode_url:
                        return existing.public_dict()
                    if existing.status == "paired":
                        return existing.public_dict()

            # Tear down previous session socket
            if self._session is not None:
                try:
                    self._session.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None

            client = LovenseSocketClient(
                token=token,
                uid=uid,
                uname=uname,
                platform=platform,
                timeout=float(env("LOVENSE_TIMEOUT", "15") or "15"),
                token_url=env("LOVENSE_TOKEN_URL", DEFAULT_TOKEN_URL) or DEFAULT_TOKEN_URL,
                socket_url_api=env("LOVENSE_SOCKET_URL_API", DEFAULT_SOCKET_URL_API)
                or DEFAULT_SOCKET_URL_API,
            )

            session = PairingSession(
                uid=uid,
                uname=uname,
                platform=platform,
                client=client,
                status="connecting",
                message="Connecting to Lovense…",
            )

            # Wire device events → re-evaluate paired
            def _on_any_event(_payload: dict) -> None:
                session.refresh_paired_state()

            client.on_device_info = _on_any_event
            client.on_app_status = _on_any_event
            client.on_app_online = _on_any_event

            self._session = session

        try:
            client.connect()
            time.sleep(0.25)
            qr_response = client.request_qrcode()
            fields = _extract_qr_fields(qr_response)
            with session._lock:
                session.qrcode_url = fields.get("qrcode_url")
                session.qrcode = fields.get("qrcode")
                session.pair_code = fields.get("code")
                if not session.qrcode_url and not session.qrcode:
                    session.status = "error"
                    session.error = "Lovense did not return a QR code."
                    session.message = session.error
                else:
                    session.status = "waiting_for_scan"
                    session.message = (
                        "Scan this QR code with Lovense Connect (or Lovense Remote)."
                    )
            # Persist uid immediately so the local controller can share identity
            upsert_env_value(ENV_PATH, "LOVENSE_UID", uid)
            upsert_env_value(ENV_PATH, "LOVENSE_UNAME", uname)
            upsert_env_value(ENV_PATH, "LOVENSE_PLATFORM", platform)
            _persist_state(session)
            session.refresh_paired_state()
            return session.public_dict()
        except LovenseError as exc:
            with session._lock:
                session.status = "error"
                session.error = str(exc)
                session.message = str(exc)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:
            with session._lock:
                session.status = "error"
                session.error = str(exc)
                session.message = str(exc)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise LovenseError(f"Pairing failed: {exc}") from exc

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                saved = _load_state()
                if saved:
                    return {
                        **saved,
                        "status": saved.get("status", "unknown"),
                        "paired": saved.get("status") == "paired",
                        "active_session": False,
                    }
                return {
                    "status": "idle",
                    "paired": False,
                    "message": "Click the button to start pairing.",
                    "active_session": False,
                }
            self._session.refresh_paired_state()
            data = self._session.public_dict()
            data["active_session"] = True
            return data

    def reset(self) -> dict[str, Any]:
        """Drop the active socket session (does not un-pair on Lovense side)."""
        with self._lock:
            if self._session is not None:
                try:
                    self._session.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None
        return self.status()


def _persist_state(session: PairingSession) -> None:
    data = session.public_dict()
    # Don't store huge raw blobs
    data.pop("raw", None)
    try:
        path = pairing_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_state() -> dict[str, Any] | None:
    path = pairing_state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def controller_identity() -> dict[str, Any]:
    """
    Payload for remote controllers (never includes LOVENSE_TOKEN).

    Controllers run on a different machine from the pairing helper. They call
    the pairing server to learn the owner's uid/platform after a successful
    web QR pair, then open their own Socket.IO session with their local copy
    of the developer token.
    """
    status = manager.status()
    # Prefer live session fields; fall back to env / saved state.
    uid = status.get("uid") or env("LOVENSE_UID")
    platform = (
        status.get("platform")
        or env("LOVENSE_PLATFORM", DEFAULT_PLATFORM)
        or DEFAULT_PLATFORM
    )
    uname = status.get("uname") or env("LOVENSE_UNAME", DEFAULT_UNAME) or DEFAULT_UNAME
    paired = bool(status.get("paired") or status.get("status") == "paired")
    return {
        "uid": uid,
        "platform": platform,
        "uname": uname,
        "paired": paired,
        "status": status.get("status", "unknown"),
        "paired_at": status.get("paired_at"),
        "message": status.get("message") or "",
        # Explicit: controllers must already have the developer token locally.
        "token_included": False,
    }


# Process-wide manager for the web app
manager = PairingManager()
