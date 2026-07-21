"""
Helpers for controllers on a different machine from the pairing web app.

The pairing server only handles owner QR pairing. Controllers (voice, motion,
…) need:
  * LOVENSE_TOKEN          — developer token (local .env on the controller)
  * uid + platform [+ uname] — fetched from the pairing server after the owner pairs
  * network access to api.lovense-api.com for Socket.IO commands

Never expect the pairing server to return LOVENSE_TOKEN.
"""

from __future__ import annotations

import sys
from typing import Any

from config import env

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class ControllerConfigError(Exception):
    """Missing or invalid controller / pairing-server configuration."""


def fetch_pairing_health(
    *,
    pairing_server_url: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """GET /api/health on the pairing helper (no API key required)."""
    if requests is None:
        raise ControllerConfigError(
            "'requests' is required. Install project deps with: uv sync"
        )
    base = (pairing_server_url or env("PAIRING_SERVER_URL") or "").rstrip("/")
    if not base:
        raise ControllerConfigError(
            "PAIRING_SERVER_URL is not set. Point it at the remote pairing helper "
            "(e.g. https://pair.example.com)."
        )
    url = f"{base}/api/health"
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise ControllerConfigError(
            f"Could not reach pairing server at {url}: {exc}"
        ) from exc
    if resp.status_code >= 400:
        detail = (resp.text or "").strip()[:300]
        raise ControllerConfigError(
            f"Health check HTTP {resp.status_code}: {detail}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise ControllerConfigError("Health endpoint returned non-JSON.") from exc
    if not isinstance(data, dict):
        raise ControllerConfigError("Invalid health payload.")
    return data


def fetch_owner_identity(
    *,
    pairing_server_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 10.0,
    require_paired: bool = True,
) -> dict[str, Any]:
    """
    GET /api/controller/identity from the remote pairing helper.

    Auth: ``Authorization: Bearer <CONTROLLER_API_KEY>`` or ``X-Api-Key``.
    """
    if requests is None:
        raise ControllerConfigError(
            "'requests' is required. Install project deps with: uv sync"
        )

    base = (pairing_server_url or env("PAIRING_SERVER_URL") or "").rstrip("/")
    key = api_key or env("CONTROLLER_API_KEY") or ""
    if not base:
        raise ControllerConfigError(
            "PAIRING_SERVER_URL is not set. Point it at the remote pairing helper "
            "(e.g. https://pair.example.com)."
        )
    if not key:
        raise ControllerConfigError(
            "CONTROLLER_API_KEY is not set. Use the same secret configured on "
            "the pairing server as CONTROLLER_API_KEY."
        )

    url = f"{base}/api/controller/identity"
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Api-Key": key,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ControllerConfigError(
            f"Could not reach pairing server at {url}: {exc}"
        ) from exc

    if resp.status_code in (401, 403):
        raise ControllerConfigError(
            "Pairing server rejected CONTROLLER_API_KEY (unauthorized)."
        )
    if resp.status_code == 404:
        raise ControllerConfigError(
            f"Pairing server has no identity endpoint at {url}."
        )
    if resp.status_code >= 400:
        detail = (resp.text or "").strip()[:300]
        raise ControllerConfigError(
            f"Pairing server returned HTTP {resp.status_code}: {detail}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ControllerConfigError(
            "Pairing server returned non-JSON identity payload."
        ) from exc

    if not isinstance(data, dict):
        raise ControllerConfigError("Invalid identity payload (expected object).")

    if require_paired and not data.get("paired"):
        raise ControllerConfigError(
            "Owner has not finished pairing yet "
            f"(status={data.get('status')!r}). "
            "Open the pairing site, click Pair, and scan with Lovense Connect."
        )
    if not data.get("uid"):
        raise ControllerConfigError(
            "Pairing server did not return a uid. Complete pairing on the web app."
        )
    if not data.get("platform"):
        raise ControllerConfigError(
            "Pairing server did not return platform."
        )
    return data


def resolve_controller_identity(
    *,
    uid: str | None = None,
    platform: str | None = None,
    uname: str | None = None,
    pairing_server_url: str | None = None,
    api_key: str | None = None,
    fetch_from_server: bool | None = None,
    require_paired: bool = True,
    timeout: float = 10.0,
) -> dict[str, str]:
    """
    Resolve uid/platform/uname for a controller.

    If ``PAIRING_SERVER_URL`` is set (or fetch_from_server=True), pull identity
    from the remote pairing helper. Local LOVENSE_UID / flags override only when
    the server is not used.
    """
    server_url = pairing_server_url or env("PAIRING_SERVER_URL")
    should_fetch = fetch_from_server
    if should_fetch is None:
        should_fetch = bool(server_url)

    if should_fetch:
        remote = fetch_owner_identity(
            pairing_server_url=server_url,
            api_key=api_key,
            timeout=timeout,
            require_paired=require_paired,
        )
        print(
            f"Fetched owner identity from pairing server: "
            f"uid={remote.get('uid')!r} platform={remote.get('platform')!r} "
            f"paired={remote.get('paired')}"
        )
        return {
            "uid": str(remote["uid"]),
            "platform": str(remote["platform"]),
            "uname": str(remote.get("uname") or env("LOVENSE_UNAME") or "BuzzLightyear"),
        }

    # Local-only (legacy / same-machine)
    resolved_uid = uid or env("LOVENSE_UID") or ""
    resolved_platform = platform or env("LOVENSE_PLATFORM") or ""
    resolved_uname = uname or env("LOVENSE_UNAME") or "BuzzLightyear"
    return {
        "uid": resolved_uid,
        "platform": resolved_platform,
        "uname": resolved_uname,
    }
