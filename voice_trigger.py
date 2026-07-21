#!/usr/bin/env python3
"""
voice_trigger.py — CLI entry point.

Wires trigger config, Lovense Standard Socket API actions, and the voice
recognition source. Other non-audio sources can reuse triggers + actions:

    engine = TriggerEngine(triggers, on_match=...)
    engine.handle_text("some text from another source")
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

import recognition
from actions import LovenseError, LovenseSocketClient, execute_trigger, pair
from config import (
    DEFAULT_ACTION,
    DEFAULT_COOLDOWN,
    DEFAULT_PLATFORM,
    DEFAULT_SOCKET_URL_API,
    DEFAULT_TIME_SEC,
    DEFAULT_TOKEN_URL,
    DEFAULT_TRIGGERS_FILE,
    DEFAULT_UNAME,
    ENV_PATH,
    env,
)
from controller_client import ControllerConfigError, resolve_controller_identity
from triggers import ActionDefaults, TriggerAction, TriggerEngine, load_triggers, parse_stop_previous


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Transcribe mic audio and send Lovense Standard Socket API commands "
            "when a phrase is heard. Secrets load from .env; phrase → action "
            "mappings load from triggers.json. Use --pair to link Lovense Remote."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--model",
        default=env("VOSK_MODEL"),
        help="Path to an unpacked Vosk model directory. Or set VOSK_MODEL in .env.",
    )
    p.add_argument(
        "--config",
        default=env("TRIGGERS_FILE", str(DEFAULT_TRIGGERS_FILE)),
        help="JSON file mapping phrases to Lovense actions.",
    )
    p.add_argument(
        "--phrase",
        action="append",
        dest="phrases",
        metavar="PHRASE",
        help=(
            "Extra trigger phrase using the default action/time. "
            "Repeatable. Prefer editing triggers.json for per-phrase outputs."
        ),
    )
    p.add_argument(
        "--token",
        default=env("LOVENSE_TOKEN"),
        help="Lovense developer token. Or set LOVENSE_TOKEN in .env.",
    )
    p.add_argument(
        "--uid",
        default=env("LOVENSE_UID"),
        help=(
            "Owner uid (optional if PAIRING_SERVER_URL is set — fetched from "
            "the remote pairing helper)."
        ),
    )
    p.add_argument(
        "--uname",
        default=env("LOVENSE_UNAME", DEFAULT_UNAME),
        help="Display name sent during Socket auth.",
    )
    p.add_argument(
        "--platform",
        default=env("LOVENSE_PLATFORM", DEFAULT_PLATFORM),
        help=(
            "Dashboard website name (optional if fetched from pairing server)."
        ),
    )
    p.add_argument(
        "--pairing-server",
        default=env("PAIRING_SERVER_URL"),
        help=(
            "Base URL of the remote pairing helper (e.g. https://pair.example.com). "
            "When set, uid/platform are fetched via /api/controller/identity."
        ),
    )
    p.add_argument(
        "--controller-api-key",
        default=env("CONTROLLER_API_KEY"),
        help="Shared secret for the pairing helper controller API.",
    )
    p.add_argument(
        "--token-url",
        default=env("LOVENSE_TOKEN_URL", DEFAULT_TOKEN_URL),
        help="Lovense getToken endpoint.",
    )
    p.add_argument(
        "--socket-url-api",
        default=env("LOVENSE_SOCKET_URL_API", DEFAULT_SOCKET_URL_API),
        help="Lovense getSocketUrl endpoint.",
    )
    p.add_argument(
        "--action",
        default=env("LOVENSE_ACTION", DEFAULT_ACTION),
        help="Default Lovense Function action (used when a trigger omits action).",
    )
    p.add_argument(
        "--time-sec",
        type=float,
        default=float(env("LOVENSE_TIME_SEC", str(DEFAULT_TIME_SEC))),
        help="Default running time in seconds (0 = indefinite).",
    )
    p.add_argument(
        "--toy",
        default=env("LOVENSE_TOY"),
        help="Default optional toy id. Omit to target all toys for the uid.",
    )
    p.add_argument(
        "--device",
        default=None,
        help="Input device index or name substring. Omit for the system default.",
    )
    p.add_argument(
        "--samplerate",
        type=int,
        default=None,
        help="Sample rate in Hz. Defaults to the device's preferred rate.",
    )
    p.add_argument(
        "--cooldown",
        type=float,
        default=float(env("LOVENSE_COOLDOWN", str(DEFAULT_COOLDOWN))),
        help="Default minimum seconds between two fires for the same phrase.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP/socket connect timeout in seconds.",
    )
    p.add_argument(
        "--pair",
        action="store_true",
        help=(
            "CLI fallback pairing (QR in terminal). Preferred: run the web app "
            "so the device owner pairs in a browser: python -m webapp"
        ),
    )
    p.add_argument(
        "--open-qr",
        action="store_true",
        help="With --pair, open the QR image URL in the default browser.",
    )
    p.add_argument(
        "--save-qr",
        metavar="PATH",
        default=None,
        help="With --pair, download the QR image to this path (e.g. pair_qr.png).",
    )
    p.add_argument(
        "--list-devices",
        action="store_true",
        help="Print available audio input devices and exit.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print partial/final transcripts and socket debug logs.",
    )
    p.add_argument(
        "--test",
        action="store_true",
        help=(
            "Test mode: run recognition and matching, but do not emit socket "
            "commands. LOVENSE_TOKEN / LOVENSE_UID are optional in this mode."
        ),
    )
    return p.parse_args(argv)


def defaults_from_args(args: argparse.Namespace) -> ActionDefaults:
    stop_raw = env("LOVENSE_STOP_PREVIOUS")
    return ActionDefaults(
        action=args.action,
        time_sec=args.time_sec,
        toy=args.toy or None,
        stop_previous=parse_stop_previous(stop_raw, context="LOVENSE_STOP_PREVIOUS"),
        cooldown=args.cooldown,
    )


def build_client(args: argparse.Namespace) -> LovenseSocketClient:
    """
    Build a Socket client for this controller machine.

    Identity (uid/platform) comes from the remote pairing helper when
    --pairing-server / PAIRING_SERVER_URL is set; the developer token is
    always local (never downloaded from the pairing server).
    """
    uid = args.uid or ""
    platform = args.platform or ""
    uname = args.uname or DEFAULT_UNAME

    if not args.test:
        try:
            identity = resolve_controller_identity(
                uid=args.uid,
                platform=args.platform,
                uname=args.uname,
                pairing_server_url=args.pairing_server,
                api_key=args.controller_api_key,
                # Fetch whenever a pairing server is configured.
                fetch_from_server=bool(args.pairing_server),
                require_paired=True,
                timeout=args.timeout,
            )
            uid = identity["uid"]
            platform = identity["platform"]
            uname = identity["uname"]
        except ControllerConfigError as exc:
            # If no pairing server configured, fall through to local env checks.
            if args.pairing_server:
                raise SystemExit(str(exc)) from exc

    missing = []
    if not args.test:
        if not args.token:
            missing.append("LOVENSE_TOKEN / --token (local developer token on this PC)")
        if not uid:
            missing.append(
                "uid (set PAIRING_SERVER_URL to fetch from pairing helper, "
                "or LOVENSE_UID)"
            )
        if not platform:
            missing.append("platform (from pairing helper or LOVENSE_PLATFORM)")
    if missing:
        raise SystemExit(
            "Missing required controller config: "
            + ", ".join(missing)
            + ".\nSee .env.example (controller section) and README architecture."
        )
    return LovenseSocketClient(
        token=args.token or "",
        uid=uid,
        uname=uname,
        platform=platform,
        timeout=args.timeout,
        token_url=args.token_url,
        socket_url_api=args.socket_url_api,
        verbose=args.verbose,
    )


def run_listen(args: argparse.Namespace) -> int:
    """Voice source → TriggerEngine → Lovense Socket API."""
    client = build_client(args)
    triggers = load_triggers(
        config=args.config,
        base=defaults_from_args(args),
        extra_phrases=args.phrases,
    )

    def on_match(trigger: TriggerAction, _text: str) -> None:
        execute_trigger(client, trigger, test_mode=args.test)

    engine = TriggerEngine(triggers, on_match)

    mode = "TEST MODE (no socket emit)" if args.test else "live (Socket API)"
    uid_display = client.uid if client.uid else "(unset)"
    print(
        f"Mode: {mode}\n"
        f"Lovense uid={uid_display!r} platform={client.platform!r}\n"
        f"Triggers: {engine.summary()}"
    )

    if not args.test:
        try:
            client.connect()
        except LovenseError as exc:
            client.close()
            raise SystemExit(str(exc)) from exc
        except SystemExit:
            client.close()
            raise

    try:
        return recognition.listen(
            model_path=args.model or "",
            on_text=engine.handle_text,
            device=args.device,
            samplerate=args.samplerate,
            verbose=args.verbose,
        )
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ENV_PATH)
    args = parse_args(argv)

    if args.list_devices:
        print(recognition.list_input_devices())
        return 0

    if args.pair:
        print(
            "Note: preferred pairing is via the web app "
            "(device owner scans QR in the browser):\n"
            "  python -m webapp\n"
        )
        try:
            return pair(
                token=args.token or "",
                uid=args.uid,
                uname=args.uname,
                platform=args.platform,
                timeout=args.timeout,
                open_qr=args.open_qr,
                save_qr=args.save_qr,
                token_url=args.token_url,
                socket_url_api=args.socket_url_api,
            )
        except LovenseError as exc:
            raise SystemExit(str(exc)) from exc

    return run_listen(args)


if __name__ == "__main__":
    sys.exit(main())
