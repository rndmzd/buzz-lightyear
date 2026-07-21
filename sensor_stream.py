"""
UDP motion sensor stream (ESP32 → host).

Packet layout (16 bytes, little-endian) — keep in sync with firmware::

    offset  size  type    field
    0       4     uint32  protocol_id  = 0x425A5531 ("BZU1")
    4       4     uint32  sequence
    8       4     uint32  device_timestamp_us
    12      4     float   value (0.0–1.0)
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Must match firmware kProtocolId / kPacketSize.
PROTOCOL_ID = 0x425A5531
PACKET_SIZE = 16
PACKET_STRUCT = struct.Struct("<I I I f")  # protocol, seq, ts_us, value

RECV_BUFFER_BYTES = 256 * 1024
DEFAULT_UDP_PORT = 5005


@dataclass(frozen=True)
class SamplePacket:
    protocol_id: int
    sequence: int
    device_timestamp_us: int
    value: float


class PacketError(ValueError):
    """Invalid or unexpected datagram payload."""


def encode_packet(
    sequence: int,
    device_timestamp_us: int,
    value: float,
    *,
    protocol_id: int = PROTOCOL_ID,
) -> bytes:
    """Serialize one sample to the wire format (little-endian)."""
    return PACKET_STRUCT.pack(protocol_id, sequence, device_timestamp_us, value)


def decode_packet(data: bytes) -> SamplePacket:
    """Decode and validate one datagram. Raises PacketError on rejection."""
    if len(data) != PACKET_SIZE:
        raise PacketError(f"unexpected size {len(data)} (want {PACKET_SIZE})")
    protocol_id, sequence, timestamp_us, value = PACKET_STRUCT.unpack(data)
    if protocol_id != PROTOCOL_ID:
        raise PacketError(
            f"bad protocol_id 0x{protocol_id:08X} (want 0x{PROTOCOL_ID:08X})"
        )
    return SamplePacket(
        protocol_id=protocol_id,
        sequence=sequence,
        device_timestamp_us=timestamp_us,
        value=value,
    )


@dataclass
class StreamStats:
    received: int = 0
    rejected: int = 0
    gaps: int = 0
    reordered: int = 0
    last_sequence: int | None = None
    jitter_samples: int = 0
    jitter_sum_us: float = 0.0
    jitter_max_us: float = 0.0
    last_device_ts: int | None = None
    window_start: float = 0.0
    window_count: int = 0

    def note_rejected(self) -> None:
        self.rejected += 1

    def note_packet(self, packet: SamplePacket, host_time: float) -> None:
        if self.window_start == 0.0:
            self.window_start = host_time
        self.window_count += 1
        self.received += 1

        if self.last_sequence is not None:
            delta = packet.sequence - self.last_sequence
            if delta > 1:
                self.gaps += delta - 1
            elif delta < 1:
                self.reordered += 1
        self.last_sequence = packet.sequence

        if self.last_device_ts is not None:
            dt = float(
                (packet.device_timestamp_us - self.last_device_ts) & 0xFFFFFFFF
            )
            if 0.0 < dt < 100_000.0:
                expected = 5000.0
                err = abs(dt - expected)
                self.jitter_sum_us += err
                self.jitter_max_us = max(self.jitter_max_us, err)
                self.jitter_samples += 1
        self.last_device_ts = packet.device_timestamp_us

    def rate_hz(self, now: float) -> float:
        elapsed = now - self.window_start
        if elapsed <= 0.0 or self.window_count <= 0:
            return 0.0
        return self.window_count / elapsed

    def mean_jitter_us(self) -> float:
        if self.jitter_samples <= 0:
            return 0.0
        return self.jitter_sum_us / self.jitter_samples

    def reset_window(self, now: float) -> None:
        self.window_start = now
        self.window_count = 0

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "received": self.received,
            "rejected": self.rejected,
            "gaps": self.gaps,
            "reordered": self.reordered,
            "rate_hz": self.rate_hz(now),
            "jitter_mean_us": self.mean_jitter_us(),
            "last_sequence": self.last_sequence,
        }


SampleCallback = Callable[[SamplePacket], None]
ErrorCallback = Callable[[str], None]


class UdpSensorReceiver:
    """
    Background UDP listener for ESP32 motion samples.

    Always keeps only the latest sample (no backlog). Thread-safe reads via
    ``latest`` / ``stats``.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_UDP_PORT,
        on_sample: SampleCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_sample = on_sample
        self.on_error = on_error

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._latest: SamplePacket | None = None
        self._latest_host_time: float = 0.0
        self._stats = StreamStats()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def latest(self) -> SamplePacket | None:
        with self._lock:
            return self._latest

    @property
    def latest_value(self) -> float | None:
        with self._lock:
            if self._latest is None:
                return None
            return self._latest.value

    @property
    def stats(self) -> StreamStats:
        with self._lock:
            # Shallow copy of counters for UI
            s = StreamStats(
                received=self._stats.received,
                rejected=self._stats.rejected,
                gaps=self._stats.gaps,
                reordered=self._stats.reordered,
                last_sequence=self._stats.last_sequence,
                jitter_samples=self._stats.jitter_samples,
                jitter_sum_us=self._stats.jitter_sum_us,
                jitter_max_us=self._stats.jitter_max_us,
                last_device_ts=self._stats.last_device_ts,
                window_start=self._stats.window_start,
                window_count=self._stats.window_count,
            )
            return s

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        except OSError:
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        # Short timeout so stop() is responsive without busy-polling.
        sock.settimeout(0.2)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="udp-sensor-receiver",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    continue

                mono = time.monotonic()
                try:
                    packet = decode_packet(data)
                except PacketError:
                    with self._lock:
                        self._stats.note_rejected()
                    continue

                with self._lock:
                    self._stats.note_packet(packet, mono)
                    self._latest = packet
                    self._latest_host_time = mono

                if self.on_sample is not None:
                    try:
                        self.on_sample(packet)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            if self.on_error is not None and not self._stop.is_set():
                try:
                    self.on_error(str(exc))
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._running = False


def map_value_to_level(
    value: float,
    *,
    max_level: int = 20,
    deadband: float = 0.0,
) -> int:
    """
    Map a normalized sensor value [0, 1] to a Lovense vibration level [0, max_level].

    Values at/below deadband map to 0.
    """
    if max_level < 0:
        max_level = 0
    if max_level > 20:
        max_level = 20
    v = float(value)
    if v < 0.0:
        v = 0.0
    elif v > 1.0:
        v = 1.0
    if v <= deadband:
        return 0
    if deadband > 0.0 and deadband < 1.0:
        v = (v - deadband) / (1.0 - deadband)
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
    return int(round(v * max_level))
