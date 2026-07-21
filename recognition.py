"""
Voice recognition source: microphone capture + offline Vosk STT.

Produces free-form transcript text and hands it to a callback. Matching
phrases and firing actions live outside this module so other trigger sources
can reuse the same pipeline.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any

TextHandler = Callable[[str], None]
"""Callback invoked with each non-empty partial or final transcript."""


class RecognitionError(Exception):
    """Microphone / model failure."""


def list_input_devices() -> Any:
    """Return the sounddevice device table (printable)."""
    import sounddevice as sd

    return sd.query_devices()


def list_input_device_choices() -> list[tuple[int, str]]:
    """Return (index, label) for devices with input channels."""
    import sounddevice as sd

    choices: list[tuple[int, str]] = []
    for idx, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) > 0:
            choices.append((idx, f"{idx}: {info['name']}"))
    return choices


def resolve_device(device: str | int | None) -> int | None:
    """Turn a device index-or-name string into a sounddevice device index."""
    import sounddevice as sd

    if device is None or device == "":
        return None
    if isinstance(device, int):
        return device
    try:
        return int(device)
    except (TypeError, ValueError):
        pass
    needle = str(device)
    for idx, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0 and needle.lower() in info["name"].lower():
            return idx
    raise RecognitionError(f"No input device matching {device!r}.")


def default_samplerate(device: int | None) -> int:
    import sounddevice as sd

    info = sd.query_devices(device, "input")
    return int(info["default_samplerate"])


class VoiceListener:
    """
    Background mic → Vosk STT loop for GUI / non-blocking use.

    Call start() / stop(). on_text is invoked from the listener thread.
    """

    def __init__(
        self,
        *,
        model_path: str,
        on_text: TextHandler,
        device: str | int | None = None,
        samplerate: int | None = None,
        blocksize: int = 8000,
        verbose: bool = False,
        on_status: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.model_path = model_path
        self.on_text = on_text
        self.device = device
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.verbose = verbose
        self.on_status = on_status
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if not self.model_path:
            raise RecognitionError(
                "Voice recognition requires a Vosk model path."
            )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="VoiceListener", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        self._thread = None

    def _emit_status(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)

    def _emit_error(self, msg: str) -> None:
        if self.on_error:
            self.on_error(msg)
        else:
            print(msg, file=sys.stderr)

    def _run(self) -> None:
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            self._emit_error(f"Missing audio/STT dependency: {exc}")
            return

        try:
            device_index = resolve_device(self.device)
            rate = (
                self.samplerate
                if self.samplerate is not None
                else default_samplerate(device_index)
            )
            SetLogLevel(-1)
            self._emit_status(f"Loading Vosk model from {self.model_path} …")
            model = Model(self.model_path)
            recognizer = KaldiRecognizer(model, rate)
            audio_q: queue.Queue[bytes] = queue.Queue()

            def audio_callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
                if status:
                    self._emit_status(f"audio: {status}")
                if not self._stop.is_set():
                    audio_q.put(bytes(indata))

            self._emit_status(
                f"Listening on device "
                f"{device_index if device_index is not None else 'default'} "
                f"@ {rate} Hz"
            )
            with sd.RawInputStream(
                samplerate=rate,
                blocksize=self.blocksize,
                device=device_index,
                dtype="int16",
                channels=1,
                callback=audio_callback,
            ):
                while not self._stop.is_set():
                    try:
                        data = audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(data):
                        result = json.loads(recognizer.Result())
                        text = result.get("text", "") or ""
                        if text:
                            if self.verbose:
                                self._emit_status(f"final: {text!r}")
                            self.on_text(text)
                    else:
                        partial = json.loads(recognizer.PartialResult())
                        text = partial.get("partial", "") or ""
                        if text:
                            if self.verbose:
                                self._emit_status(f"partial: {text!r}")
                            self.on_text(text)
            self._emit_status("Listening stopped.")
        except Exception as exc:  # noqa: BLE001
            self._emit_error(f"Voice listener error: {exc}")


def listen(
    *,
    model_path: str,
    on_text: TextHandler,
    device: str | int | None = None,
    samplerate: int | None = None,
    blocksize: int = 8000,
    verbose: bool = False,
) -> int:
    """
    Capture mic audio, transcribe with Vosk, and call on_text for each transcript.

    Blocks until KeyboardInterrupt. Returns a process exit code.
    """
    listener = VoiceListener(
        model_path=model_path,
        on_text=on_text,
        device=device,
        samplerate=samplerate,
        blocksize=blocksize,
        verbose=verbose,
    )
    try:
        listener.start()
        # Keep main thread alive until Ctrl+C
        while listener.running:
            try:
                listener._thread.join(0.5)  # type: ignore[union-attr]
            except KeyboardInterrupt:
                print("\nStopped.")
                listener.stop()
                return 0
        return 0
    except RecognitionError as exc:
        raise SystemExit(str(exc)) from exc
