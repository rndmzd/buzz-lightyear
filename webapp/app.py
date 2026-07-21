"""
Flask web app: pairing helper for the Lovense device owner.

This process should run only on the remote pairing server. Controllers
(voice_trigger, etc.) run on other machines and call
``GET /api/controller/identity`` (API key) to learn uid/platform after pair.

Architecture
------------
* Device owner browser → this site → QR → Lovense Connect scan
* Developer token stays on this server (and separately on each controller)
* Controllers never scrape .env from this host; they use the controller API
"""

from __future__ import annotations

import hmac
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from actions import LovenseError
from config import DEFAULT_PLATFORM, DEFAULT_UNAME, env
from pairing import controller_identity, manager

WEBAPP_DIR = Path(__file__).resolve().parent


def _extract_api_key() -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    key = request.headers.get("X-Api-Key") or request.args.get("api_key")
    return key.strip() if key else None


def _controller_authorized() -> bool:
    expected = env("CONTROLLER_API_KEY")
    if not expected:
        return False
    provided = _extract_api_key()
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(WEBAPP_DIR / "templates"),
        static_folder=str(WEBAPP_DIR / "static"),
    )
    app.config["JSON_SORT_KEYS"] = False

    @app.get("/")
    def index():
        return render_template(
            "pair.html",
            platform=env("LOVENSE_PLATFORM", DEFAULT_PLATFORM) or DEFAULT_PLATFORM,
            default_uname=env("LOVENSE_UNAME", DEFAULT_UNAME) or DEFAULT_UNAME,
            has_token=bool(env("LOVENSE_TOKEN")),
        )

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "role": "pairing_helper",
                "has_token": bool(env("LOVENSE_TOKEN")),
                "has_controller_api_key": bool(env("CONTROLLER_API_KEY")),
                "platform": env("LOVENSE_PLATFORM", DEFAULT_PLATFORM),
            }
        )

    @app.get("/api/controller/identity")
    def api_controller_identity():
        """
        For remote controllers only.

        Returns uid / platform / uname / paired — never LOVENSE_TOKEN.
        """
        if not env("CONTROLLER_API_KEY"):
            return (
                jsonify(
                    {
                        "error": "CONTROLLER_API_KEY is not configured on the pairing server.",
                    }
                ),
                503,
            )
        if not _controller_authorized():
            return jsonify({"error": "Unauthorized"}), 401

        identity = controller_identity()
        return jsonify(identity)

    @app.get("/api/pairing/status")
    def pairing_status():
        # Owner UI polling — no secret (does not include developer token).
        return jsonify(manager.status())

    @app.post("/api/pairing/start")
    def pairing_start():
        body = request.get_json(silent=True) or {}
        uname = (body.get("uname") or request.form.get("uname") or "").strip() or None
        force_new = bool(body.get("force_new") or request.args.get("force_new"))
        try:
            result = manager.start(uname=uname, force_new=force_new)
            return jsonify(result)
        except LovenseError as exc:
            return jsonify({"status": "error", "error": str(exc), "paired": False}), 400
        except Exception as exc:  # noqa: BLE001
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": f"Unexpected error: {exc}",
                        "paired": False,
                    }
                ),
                500,
            )

    @app.post("/api/pairing/reset")
    def pairing_reset():
        return jsonify(manager.reset())

    return app
