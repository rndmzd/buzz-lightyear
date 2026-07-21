"""
Action backends — Lovense Standard Socket API.

Flow:
  1. POST /api/basicApi/getToken      (developer token + uid)  [server only]
  2. POST /api/basicApi/getSocketUrl  (platform + authToken)
  3. Socket.IO connect (websocket)
  4. Emit basicapi_send_toy_command_ts for Function commands
  5. Emit basicapi_get_qrcode_ts for pairing QR (web app for device owner)

Trigger sources obtain a TriggerAction and call LovenseSocketClient.send_trigger.
Pairing for the device owner is done via the web app (see webapp/), not the CLI.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import (
    DEFAULT_PLATFORM,
    DEFAULT_TOKEN_URL,
    DEFAULT_SOCKET_URL_API,
    DEFAULT_UNAME,
    DEFAULT_UTOKEN_SALT,
    ENV_PATH,
    env,
    upsert_env_value,
)
from triggers import TriggerAction, describe_trigger

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import socketio
    from socketio import packet as socketio_packet
except ImportError:  # pragma: no cover
    socketio = None
    socketio_packet = None  # type: ignore[assignment]


class LovenseError(Exception):
    """Recoverable Lovense API / socket failure."""


# Socket.IO event names (Standard Socket API)
EVENT_SEND_COMMAND = "basicapi_send_toy_command_ts"
EVENT_GET_QRCODE = "basicapi_get_qrcode_ts"
EVENT_QRCODE_RESULT = "basicapi_get_qrcode_tc"
EVENT_DEVICE_INFO = "basicapi_update_device_info_tc"
EVENT_APP_STATUS = "basicapi_update_app_status_tc"
EVENT_APP_ONLINE = "basicapi_update_app_online_tc"

EventCallback = Callable[[dict], None]


def _decode_one_socketio_packet(encoded: str) -> tuple[Any, int, int]:
    """
    Decode a single Socket.IO packet from ``encoded``.

    Lovense's developer.io hub sometimes concatenates multiple Socket.IO
    packets inside one Engine.IO message (e.g. ``0{}2["event",…]``). Stock
    ``python-socketio`` uses ``json.loads`` on the remainder and raises
    ``JSONDecodeError: Extra data`` (often at column 3 / char 2 when the first
    payload is ``{}``).

    Returns ``(packet, attachment_count, chars_consumed)``.
    """
    if socketio_packet is None:  # pragma: no cover
        raise RuntimeError("python-socketio is not installed")

    ep = encoded
    original_len = len(ep)
    try:
        packet_type = int(ep[0:1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"invalid socket.io packet type in {encoded[:40]!r}") from exc
    ep = ep[1:]

    attachment_count = 0
    dash = ep.find("-")
    if dash > 0 and ep[0:dash].isdigit():
        if dash > 10:
            raise ValueError("too many attachments")
        attachment_count = int(ep[0:dash])
        ep = ep[dash + 1 :]

    namespace = None
    if ep and ep[0:1] == "/":
        sep = ep.find(",")
        if sep == -1:
            namespace = ep
            ep = ""
        else:
            namespace = ep[0:sep]
            ep = ep[sep + 1 :]
        q = namespace.find("?")
        if q != -1:
            namespace = namespace[0:q]

    pkt_id = None
    if ep and ep[0].isdigit():
        i = 1
        end = len(ep)
        while i < end:
            if not ep[i].isdigit() or i >= 100:
                break
            i += 1
        pkt_id = int(ep[:i])
        ep = ep[i:]
        if ep and ep[0].isdigit():
            raise ValueError("id field is too long")

    data = None
    if ep:
        # raw_decode consumes only the first JSON value; leftover is the next packet.
        data, idx = json.JSONDecoder().raw_decode(ep)
        ep = ep[idx:]

    pkt = socketio_packet.Packet(
        packet_type=packet_type,
        data=data,
        namespace=namespace,
        id=pkt_id,
        binary=False,
    )
    # Preserve wire type for BINARY_* attachment handling.
    pkt.packet_type = packet_type
    pkt.attachment_count = attachment_count
    pkt.attachments = []
    consumed = original_len - len(ep)
    return pkt, attachment_count, consumed


def _skip_harmless_socket_remainder(remaining: str) -> str | None:
    """
    Drop known-harmless trailing fragments from Lovense multi-packet frames.

    After a valid packet they sometimes append ``,"Invalid namespace"`` (not a
    full Socket.IO packet). That is noise about a secondary namespace probe;
    the default ``/`` namespace still works (device info / app online continue).

    Returns the trimmed remainder, or ``None`` if the whole remainder should be
    discarded without further warnings.
    """
    text = remaining.lstrip()
    if not text:
        return ""

    # ,"Invalid namespace"  or  ,"Invalid namespace"<more>
    if text.startswith(","):
        rest = text[1:].lstrip()
        if rest.startswith('"Invalid namespace"') or rest.startswith(
            "'Invalid namespace'"
        ):
            try:
                _, idx = json.JSONDecoder().raw_decode(rest)
                return rest[idx:].lstrip()
            except json.JSONDecodeError:
                return ""
        if rest.lower().startswith("invalid namespace"):
            return ""

    # Bare leftover without a leading packet type digit
    if not text[0].isdigit() and "invalid namespace" in text[:48].lower():
        return ""

    return remaining


def _make_resilient_socketio_client(**kwargs: Any) -> Any:
    """
    python-socketio Client that tolerates Lovense multi-packet Engine.IO frames.

    Also soft-fails binary attachment glitches (``packet queue is empty``) instead
    of killing the background engineio read thread.
    """
    if socketio is None:  # pragma: no cover
        raise LovenseError("python-socketio is not installed")

    class _ResilientClient(socketio.Client):  # type: ignore[misc]
        def _handle_eio_message(self, data: Any) -> None:  # noqa: ANN401
            # Binary attachment path (follow-up frames after BINARY_EVENT/ACK).
            if self._binary_packet is not None:
                try:
                    super()._handle_eio_message(data)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  [warn] socket binary attachment ignored: {exc}",
                        file=sys.stderr,
                    )
                    self._binary_packet = None
                return

            # Non-text frames: defer to stock client.
            if not isinstance(data, str):
                try:
                    super()._handle_eio_message(data)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  [warn] socket packet ignored: {exc}",
                        file=sys.stderr,
                    )
                    self._binary_packet = None
                return

            remaining = data
            # Hard cap: avoid infinite loops on corrupt streams.
            for _ in range(32):
                if not remaining:
                    return

                skipped = _skip_harmless_socket_remainder(remaining)
                if skipped is None or skipped == "":
                    return
                if skipped is not remaining:
                    remaining = skipped
                    if not remaining:
                        return

                try:
                    pkt, attachment_count, consumed = _decode_one_socketio_packet(
                        remaining
                    )
                except Exception as exc:  # noqa: BLE001
                    # Second chance: remainder may be noise we did not recognize.
                    cleaned = _skip_harmless_socket_remainder(remaining)
                    if cleaned is None or cleaned == "" or cleaned != remaining:
                        return
                    print(
                        f"  [warn] socket packet decode failed: {exc} "
                        f"prefix={remaining[:64]!r}",
                        file=sys.stderr,
                    )
                    return

                if consumed <= 0:
                    print(
                        f"  [warn] socket packet made no progress; dropping "
                        f"prefix={remaining[:64]!r}",
                        file=sys.stderr,
                    )
                    return
                remaining = remaining[consumed:]

                if pkt.packet_type == socketio_packet.CONNECT:
                    self._handle_connect(pkt.namespace, pkt.data)
                elif pkt.packet_type == socketio_packet.DISCONNECT:
                    self._handle_disconnect(pkt.namespace)
                elif pkt.packet_type == socketio_packet.EVENT:
                    self._handle_event(pkt.namespace, pkt.id, pkt.data)
                elif pkt.packet_type == socketio_packet.ACK:
                    self._handle_ack(pkt.namespace, pkt.id, pkt.data)
                elif pkt.packet_type in (
                    socketio_packet.BINARY_EVENT,
                    socketio_packet.BINARY_ACK,
                ):
                    pkt.attachment_count = attachment_count
                    self._binary_packet = pkt
                    # Binary payload(s) arrive in subsequent Engine.IO messages.
                    # Trailing text in this frame is almost always noise.
                    return
                elif pkt.packet_type == socketio_packet.CONNECT_ERROR:
                    # Lovense occasionally errors a non-default namespace; ignore.
                    err = pkt.data
                    if isinstance(err, str) and "invalid namespace" in err.lower():
                        continue
                    if (
                        isinstance(err, dict)
                        and "invalid namespace"
                        in str(err.get("message", "")).lower()
                    ):
                        continue
                    self._handle_error(pkt.namespace, pkt.data)
                else:
                    print(
                        f"  [warn] unknown socket packet type {pkt.packet_type}",
                        file=sys.stderr,
                    )

    return _ResilientClient(**kwargs)


def _parse_socket_payload(data: Any) -> Any:
    """Lovense often returns JSON strings on socket events."""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
    return data


def make_utoken(uid: str) -> str:
    """Build the utoken Lovense expects (app-local verification secret)."""
    explicit = env("LOVENSE_UTOKEN")
    if explicit:
        return explicit
    salt = env("LOVENSE_UTOKEN_SALT", DEFAULT_UTOKEN_SALT) or DEFAULT_UTOKEN_SALT
    return hashlib.md5(f"{uid}{salt}".encode("utf-8")).hexdigest()


def generate_uid() -> str:
    """Create a local user id if none is configured."""
    return f"buzz-{secrets.token_hex(4)}"


def build_function_payload(trigger: TriggerAction) -> dict[str, Any]:
    """Build a Function command body (same fields as local / server Function API)."""
    payload: dict[str, Any] = {
        "command": "Function",
        "action": trigger.action,
        "timeSec": trigger.time_sec,
        "apiVer": 1,
    }
    if trigger.toy:
        payload["toy"] = trigger.toy
    if trigger.stop_previous is not None:
        payload["stopPrevious"] = trigger.stop_previous
    return payload


def build_intensity_payload(
    level: int,
    *,
    time_sec: float = 1.0,
    stop_previous: int = 1,
    toy: str | None = None,
) -> dict[str, Any]:
    """
    Continuous-style vibration command for sensor streaming.

    ``level`` is clamped to 0–20 (Lovense Function scale). Level 0 uses Stop.
    ``stopPrevious=1`` replaces any in-progress Function so intensity can track
    a live stream without stacking commands.
    """
    level = max(0, min(20, int(level)))
    if level <= 0:
        action = "Stop"
        time_sec = 0.0
    else:
        action = f"Vibrate:{level}"
    payload: dict[str, Any] = {
        "command": "Function",
        "action": action,
        "timeSec": float(time_sec),
        "stopPrevious": int(stop_previous),
        "apiVer": 1,
    }
    if toy:
        payload["toy"] = toy
    return payload


def fetch_auth_token(
    *,
    token: str,
    uid: str,
    uname: str | None = None,
    utoken: str | None = None,
    token_url: str = DEFAULT_TOKEN_URL,
    timeout: float = 10.0,
) -> str:
    """Exchange developer token + uid for a short-lived authToken."""
    if requests is None:
        raise LovenseError(
            "'requests' is required. Install project deps with: uv sync"
        )
    body: dict[str, Any] = {"token": token, "uid": uid}
    if uname:
        body["uname"] = uname
    if utoken:
        body["utoken"] = utoken
    try:
        resp = requests.post(token_url, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise LovenseError(f"getToken failed: {exc}") from exc
    except ValueError as exc:
        raise LovenseError(f"getToken returned non-JSON: {exc}") from exc

    code = data.get("code")
    if code not in (0, "0", 200, "200", None):
        raise LovenseError(
            f"getToken error: {json.dumps(data, ensure_ascii=False)}"
        )
    auth = (data.get("data") or {}).get("authToken") or data.get("authToken")
    if not auth:
        raise LovenseError(
            f"getToken response missing authToken: "
            f"{json.dumps(data, ensure_ascii=False)}"
        )
    return str(auth)


def fetch_socket_endpoint(
    *,
    platform: str,
    auth_token: str,
    socket_url_api: str = DEFAULT_SOCKET_URL_API,
    timeout: float = 10.0,
) -> tuple[str, str]:
    """Return (socketIoUrl, socketIoPath) for Socket.IO connect."""
    if requests is None:
        raise LovenseError(
            "'requests' is required. Install project deps with: uv sync"
        )
    body = {"platform": platform, "authToken": auth_token}
    try:
        resp = requests.post(socket_url_api, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise LovenseError(f"getSocketUrl failed: {exc}") from exc
    except ValueError as exc:
        raise LovenseError(f"getSocketUrl returned non-JSON: {exc}") from exc

    code = data.get("code")
    if code not in (0, "0", 200, "200", None):
        raise LovenseError(
            f"getSocketUrl error: {json.dumps(data, ensure_ascii=False)}"
        )
    payload = data.get("data") or {}
    url = payload.get("socketIoUrl")
    path = payload.get("socketIoPath") or "/socket.io"
    if not url:
        raise LovenseError(
            f"getSocketUrl response missing socketIoUrl: "
            f"{json.dumps(data, ensure_ascii=False)}"
        )
    return str(url), str(path)


@dataclass
class LovenseSocketClient:
    """
    Persistent Socket.IO client for Lovense Standard Socket API (server path).

    Prefer one instance for the process lifetime; reconnect is handled on send
    if the socket drops.
    """

    token: str
    uid: str
    uname: str = DEFAULT_UNAME
    platform: str = DEFAULT_PLATFORM
    timeout: float = 10.0
    token_url: str = DEFAULT_TOKEN_URL
    socket_url_api: str = DEFAULT_SOCKET_URL_API
    verbose: bool = False
    # Optional hooks used by the web pairing UI
    on_device_info: EventCallback | None = field(default=None, repr=False)
    on_app_status: EventCallback | None = field(default=None, repr=False)
    on_app_online: EventCallback | None = field(default=None, repr=False)

    _sio: Any = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _socket_io_url: str | None = field(default=None, init=False, repr=False)
    _socket_io_path: str | None = field(default=None, init=False, repr=False)
    _last_device_info: dict | None = field(default=None, init=False, repr=False)
    _last_app_status: dict | None = field(default=None, init=False, repr=False)
    _last_app_online: dict | None = field(default=None, init=False, repr=False)
    _qr_waiters: dict[str, threading.Event] = field(
        default_factory=dict, init=False, repr=False
    )
    _qr_results: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def connect(self) -> None:
        """Authenticate and open the Socket.IO connection."""
        with self._lock:
            if self._connected and self._sio is not None and self._sio.connected:
                return
            self._open_socket()

    def close(self) -> None:
        """Disconnect Socket.IO if connected."""
        with self._lock:
            self._teardown_socket()

    def __enter__(self) -> LovenseSocketClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open_socket(self) -> None:
        if socketio is None:
            raise LovenseError(
                "'python-socketio' is required for the Socket API. "
                "Install project deps with: uv sync"
            )
        if not self.token or not self.uid:
            raise LovenseError(
                "Lovense Socket API requires LOVENSE_TOKEN and LOVENSE_UID."
            )
        if not self.platform:
            raise LovenseError(
                "Lovense Socket API requires platform name "
                "(LOVENSE_PLATFORM) matching the developer dashboard website name."
            )

        utoken = make_utoken(self.uid)
        print("Authenticating with Lovense Socket API …")
        auth_token = fetch_auth_token(
            token=self.token,
            uid=self.uid,
            uname=self.uname,
            utoken=utoken,
            token_url=self.token_url,
            timeout=self.timeout,
        )
        socket_url, socket_path = fetch_socket_endpoint(
            platform=self.platform,
            auth_token=auth_token,
            socket_url_api=self.socket_url_api,
            timeout=self.timeout,
        )
        self._socket_io_url = socket_url
        self._socket_io_path = socket_path
        print(f"  socket = {socket_url} path={socket_path}")

        # Lovense documents Socket.IO client 2.x; websocket-only is required.
        # Use a resilient client: their hub sometimes concatenates packets in one
        # Engine.IO frame (python-socketio's stock decoder raises JSONDecodeError).
        sio = _make_resilient_socketio_client(
            reconnection=True,
            reconnection_attempts=5,
            reconnection_delay=1,
            logger=self.verbose,
            engineio_logger=self.verbose,
        )
        self._register_handlers(sio)
        try:
            sio.connect(
                socket_url,
                socketio_path=socket_path,
                transports=["websocket"],
                wait_timeout=self.timeout,
            )
        except Exception as exc:
            raise LovenseError(f"Socket.IO connect failed: {exc}") from exc

        self._sio = sio
        self._connected = True
        print("  Socket connected.")

    def _teardown_socket(self) -> None:
        sio = self._sio
        self._sio = None
        self._connected = False
        if sio is not None:
            try:
                if sio.connected:
                    sio.disconnect()
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] socket disconnect: {exc}", file=sys.stderr)

    def _register_handlers(self, sio: Any) -> None:
        @sio.on(EVENT_QRCODE_RESULT)
        def _on_qr(data: Any) -> None:
            payload = _parse_socket_payload(data)
            ack_id = None
            if isinstance(payload, dict):
                inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                ack_id = (inner or {}).get("ackId") or payload.get("ackId")
            if ack_id and ack_id in self._qr_waiters:
                self._qr_results[ack_id] = payload
                self._qr_waiters[ack_id].set()
            elif self.verbose:
                print(f"  [socket] QR result: {payload}")

        @sio.on(EVENT_DEVICE_INFO)
        def _on_device(data: Any) -> None:
            payload = _parse_socket_payload(data)
            if isinstance(payload, dict):
                self._last_device_info = payload
                online = payload.get("online")
                toys = payload.get("toyList") or payload.get("toys")
                print(f"  [socket] device info online={online} toys={toys}")
                if self.on_device_info:
                    self.on_device_info(payload)

        @sio.on(EVENT_APP_STATUS)
        def _on_app_status(data: Any) -> None:
            payload = _parse_socket_payload(data)
            if isinstance(payload, dict):
                self._last_app_status = payload
            print(f"  [socket] app status: {payload}")
            if isinstance(payload, dict) and self.on_app_status:
                self.on_app_status(payload)

        @sio.on(EVENT_APP_ONLINE)
        def _on_app_online(data: Any) -> None:
            payload = _parse_socket_payload(data)
            if isinstance(payload, dict):
                self._last_app_online = payload
            print(f"  [socket] app online: {payload}")
            if isinstance(payload, dict) and self.on_app_online:
                self.on_app_online(payload)

        @sio.event
        def connect() -> None:
            self._connected = True
            if self.verbose:
                print("  [socket] connected")

        @sio.event
        def disconnect() -> None:
            self._connected = False
            if self.verbose:
                print("  [socket] disconnected")

    def _ensure_connected(self) -> None:
        with self._lock:
            if self._sio is not None and self._sio.connected:
                return
            self._teardown_socket()
            self._open_socket()

    def send_function(self, payload: dict[str, Any]) -> None:
        """Emit a toy Function/Pattern/Preset command via the socket."""
        self._ensure_connected()
        assert self._sio is not None
        try:
            self._sio.emit(EVENT_SEND_COMMAND, payload)
        except Exception as exc:
            print(f"  [error] socket command failed: {exc}", file=sys.stderr)
            # One reconnect retry
            try:
                with self._lock:
                    self._teardown_socket()
                    self._open_socket()
                assert self._sio is not None
                self._sio.emit(EVENT_SEND_COMMAND, payload)
            except Exception as exc2:
                print(f"  [error] socket command retry failed: {exc2}", file=sys.stderr)

    def send_trigger(self, trigger: TriggerAction, *, test_mode: bool = False) -> None:
        """Execute a matched trigger (Function command) or log in test mode."""
        payload = build_function_payload(trigger)
        if test_mode:
            print(
                f"  [test] Matched {trigger.phrase!r} — would emit "
                f"{EVENT_SEND_COMMAND} ({describe_trigger(trigger)})"
            )
            return
        self.send_function(payload)
        print(
            f"  -> socket {EVENT_SEND_COMMAND} for {trigger.phrase!r}: "
            f"{describe_trigger(trigger)}"
        )

    def send_intensity(
        self,
        level: int,
        *,
        time_sec: float = 1.0,
        stop_previous: int = 1,
        toy: str | None = None,
        test_mode: bool = False,
    ) -> dict[str, Any]:
        """
        Map a 0–20 intensity to a Function command (sensor / continuous control).

        Returns the payload that was (or would be) emitted.
        """
        payload = build_intensity_payload(
            level,
            time_sec=time_sec,
            stop_previous=stop_previous,
            toy=toy,
        )
        if test_mode:
            print(
                f"  [test] would emit {EVENT_SEND_COMMAND} "
                f"action={payload.get('action')!r} timeSec={payload.get('timeSec')}"
            )
            return payload
        self.send_function(payload)
        return payload

    def request_qrcode(self, *, ack_id: str | None = None) -> dict[str, Any]:
        """
        Request a pairing QR over the socket (basicapi_get_qrcode_ts/tc).

        Returns the parsed response payload (expects data.qrcodeUrl / data.qrcode).
        """
        self._ensure_connected()
        assert self._sio is not None
        ack = ack_id or secrets.token_hex(8)
        event = threading.Event()
        self._qr_waiters[ack] = event
        self._qr_results.pop(ack, None)
        try:
            self._sio.emit(EVENT_GET_QRCODE, {"ackId": ack})
            if not event.wait(timeout=self.timeout):
                raise LovenseError(
                    f"Timed out waiting for {EVENT_QRCODE_RESULT} "
                    f"(ackId={ack}). Is the socket connected?"
                )
            result = self._qr_results.get(ack) or {}
            if not isinstance(result, dict):
                raise LovenseError(f"Unexpected QR response: {result!r}")
            return result
        finally:
            self._qr_waiters.pop(ack, None)
            self._qr_results.pop(ack, None)

    @property
    def last_device_info(self) -> dict | None:
        return self._last_device_info

    @property
    def last_app_status(self) -> dict | None:
        return self._last_app_status

    @property
    def last_app_online(self) -> dict | None:
        return self._last_app_online

    def is_user_paired(self) -> bool:
        """
        Best-effort: True after Lovense reports the mobile/PC app is linked
        and/or a device is online for this uid.
        """
        device = self._last_device_info
        if isinstance(device, dict):
            if device.get("online") is True:
                return True
            toys = device.get("toyList") or device.get("toys") or []
            if isinstance(toys, list) and any(
                isinstance(t, dict) and t.get("connected") for t in toys
            ):
                return True
            if isinstance(toys, dict) and toys:
                return True

        for blob in (self._last_app_status, self._last_app_online):
            if not isinstance(blob, dict):
                continue
            # Accept a few shapes Lovense has used in the wild
            if blob.get("online") is True or blob.get("status") in (
                "online",
                "connected",
                "ok",
                1,
                "1",
            ):
                return True
            data = blob.get("data")
            if isinstance(data, dict) and (
                data.get("online") is True
                or data.get("status") in ("online", "connected", "ok")
            ):
                return True
        return False


# Back-compat alias used by the CLI
LovenseConnection = LovenseSocketClient


def execute_trigger(
    conn: LovenseSocketClient,
    trigger: TriggerAction,
    headers: dict[str, str] | None = None,  # noqa: ARG001 — kept for call-site compat
    *,
    test_mode: bool = False,
) -> None:
    """Run the action for a matched trigger over the Socket API."""
    conn.send_trigger(trigger, test_mode=test_mode)


def download_qr_image(image_url: str, dest: Path, timeout: float = 10.0) -> None:
    if requests is None:
        raise LovenseError("'requests' is required to download the QR image.")
    try:
        resp = requests.get(image_url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LovenseError(f"Failed to download QR image: {exc}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)


def pair(
    *,
    token: str,
    uid: str | None = None,
    uname: str | None = None,
    platform: str | None = None,
    timeout: float = 10.0,
    open_qr: bool = False,
    save_qr: str | Path | None = None,
    env_path: Path = ENV_PATH,
    token_url: str = DEFAULT_TOKEN_URL,
    socket_url_api: str = DEFAULT_SOCKET_URL_API,
) -> int:
    """
    CLI fallback pairing via Socket API.

    Preferred path: run the web app so the device owner pairs in a browser
    (``python -m webapp``).

    Returns a process exit code (0 = success).
    """
    if not token:
        raise LovenseError(
            "Pairing requires LOVENSE_TOKEN / --token.\n"
            "Copy it from the Lovense developer dashboard into .env."
        )

    uid_was_generated = not uid
    uid = uid or generate_uid()
    uname = uname or DEFAULT_UNAME
    platform = platform or env("LOVENSE_PLATFORM", DEFAULT_PLATFORM) or DEFAULT_PLATFORM

    client = LovenseSocketClient(
        token=token,
        uid=uid,
        uname=uname,
        platform=platform,
        timeout=timeout,
        token_url=token_url,
        socket_url_api=socket_url_api,
    )

    print("Pairing via Lovense Standard Socket API …")
    print(f"  uid      = {uid}")
    print(f"  uname    = {uname}")
    print(f"  platform = {platform}")

    try:
        client.connect()
        # Brief settle so listeners are attached before the emit.
        time.sleep(0.2)
        data = client.request_qrcode()
    except LovenseError:
        client.close()
        raise
    except Exception as exc:
        client.close()
        raise LovenseError(f"Pairing failed: {exc}") from exc

    message = data.get("message") or ""
    code = data.get("code")
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    qr_image = None
    qr_raw = None
    pair_code = None
    if isinstance(body, dict):
        qr_image = body.get("qrcodeUrl") or body.get("qr") or body.get("qrcode_url")
        qr_raw = body.get("qrcode")
        pair_code = body.get("code") or body.get("deviceCode")

    print()
    if code not in (0, "0", 200, "200", None):
        print(f"Lovense response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        print(
            "Pairing may have failed. Check LOVENSE_TOKEN, LOVENSE_PLATFORM "
            "(must match dashboard website name), and Callback URL.",
            file=sys.stderr,
        )
        client.close()
        return 1

    if message:
        print(f"Lovense: {message}")

    print()
    print("=== Pairing (Socket API) ===")
    print(f"App user id (LOVENSE_UID): {uid}")
    if pair_code:
        print(f"Code:                      {pair_code}")
    if qr_image:
        print(f"QR image URL:              {qr_image}")
        print("  Mobile: open Lovense Remote → scan this QR code.")
    if qr_raw and not qr_image:
        print(f"QR raw payload:            {str(qr_raw)[:120]}…")
        print("  (Generate a QR image from this payload if needed.)")
    if not qr_image and not qr_raw:
        print("Full response:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

    print()
    print("After a successful scan, watch for device-info / app-status socket")
    print("events. Keep Lovense Remote online with the toy connected.")
    print()

    upsert_env_value(env_path, "LOVENSE_UID", uid)
    if uname:
        upsert_env_value(env_path, "LOVENSE_UNAME", uname)
    if platform:
        upsert_env_value(env_path, "LOVENSE_PLATFORM", platform)
    if token and not env("LOVENSE_TOKEN"):
        upsert_env_value(env_path, "LOVENSE_TOKEN", token)

    print(f"Wrote LOVENSE_UID={uid} to {env_path}")
    if uid_was_generated:
        print("  (Generated a new uid because none was set.)")

    saved_path: Path | None = None
    if qr_image and save_qr:
        dest = Path(save_qr).expanduser()
        if not dest.is_absolute():
            dest = Path.cwd() / dest
        if dest.exists() and dest.is_dir():
            dest = dest / "lovense_pair_qr.png"
        elif dest.suffix == "":
            dest = dest.with_suffix(".png")
        download_qr_image(str(qr_image), dest, timeout)
        saved_path = dest
        print(f"Saved QR image to {dest}")

    if qr_image and open_qr:
        if saved_path and saved_path.is_file():
            webbrowser.open(saved_path.resolve().as_uri())
        else:
            webbrowser.open(str(qr_image))
        print("Opened QR in the default browser.")

    # Leave the socket up briefly so device-info events can arrive after scan.
    print()
    print("Waiting 30s for scan / device events (Ctrl+C to skip) …")
    try:
        for _ in range(30):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nSkipped wait.")

    client.close()

    print()
    print("Next steps:")
    print("  1. Confirm Lovense Remote is online with the toy connected.")
    print("  2. Run the listener, e.g.:")
    print("       python voice_trigger.py --model ./vosk-model-small-en-us-0.15 --test")
    return 0
