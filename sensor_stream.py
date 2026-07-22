"""
UDP motion sensor stream (ESP32 → host).

Packet layout (16 bytes, little-endian) — keep in sync with firmware::

    offset  size  type    field
    0       4     uint32  protocol_id  = 0x425A5531 ("BZU1")
    4       4     uint32  sequence
    8       4     uint32  device_timestamp_us
    12      4     float   value (0.0–1.0)

CSV recordings (record / replay) use the same columns as tools/udp_receiver.py::

    host_timestamp, device_timestamp_us, sequence, value
"""

from __future__ import annotations

import csv
import socket
import struct
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

# Must match firmware kProtocolId / kPacketSize.
PROTOCOL_ID = 0x425A5531
PACKET_SIZE = 16
PACKET_STRUCT = struct.Struct("<I I I f")  # protocol, seq, ts_us, value

RECV_BUFFER_BYTES = 256 * 1024
DEFAULT_UDP_PORT = 5005

# Recording CSV (compatible with tools/udp_receiver.py --csv).
RECORDING_CSV_HEADER = [
    "host_timestamp",
    "device_timestamp_us",
    "sequence",
    "value",
]
CSV_FLUSH_EVERY = 50


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


# ---------------------------------------------------------------------------
# Record / replay (simulate motion without a live ESP32)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedSample:
    """One sample from a CSV recording."""

    host_timestamp: float
    """Wall-clock time when the host received/stored the sample (seconds)."""

    packet: SamplePacket

    @property
    def value(self) -> float:
        return self.packet.value


class RecordingError(ValueError):
    """Invalid or unreadable sensor recording."""


class SensorRecorder:
    """
    Append decoded samples to a CSV file (same schema as tools/udp_receiver.py).

    Thread-safe: safe to call :meth:`record` from a UDP receive callback.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: TextIO | None = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(RECORDING_CSV_HEADER)
        self._fh.flush()
        self._count = 0
        self._pending = 0
        self._closed = False

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def record(
        self,
        packet: SamplePacket,
        *,
        host_timestamp: float | None = None,
    ) -> None:
        """Write one sample. ``host_timestamp`` defaults to ``time.time()``."""
        ts = time.time() if host_timestamp is None else float(host_timestamp)
        with self._lock:
            if self._closed or self._fh is None:
                raise RecordingError("Recorder is closed.")
            self._writer.writerow(
                [
                    f"{ts:.6f}",
                    packet.device_timestamp_us,
                    packet.sequence,
                    f"{packet.value:.6f}",
                ]
            )
            self._count += 1
            self._pending += 1
            if self._pending >= CSV_FLUSH_EVERY:
                self._fh.flush()
                self._pending = 0

    def close(self) -> int:
        """Flush and close. Returns total samples written."""
        with self._lock:
            if self._closed:
                return self._count
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError:
                    pass
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            self._closed = True
            return self._count


def load_recording_csv(path: str | Path) -> list[RecordedSample]:
    """
    Load a sensor CSV recording.

    Accepts the header from :data:`RECORDING_CSV_HEADER` (or the same columns
    in any order). Rows missing required fields are skipped with a count.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise RecordingError(f"Recording not found: {file_path}")

    samples: list[RecordedSample] = []
    try:
        with file_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise RecordingError(f"Empty or header-less CSV: {file_path}")
            fields = {name.strip().lower(): name for name in reader.fieldnames if name}
            required = (
                "host_timestamp",
                "device_timestamp_us",
                "sequence",
                "value",
            )
            missing = [k for k in required if k not in fields]
            if missing:
                raise RecordingError(
                    f"{file_path}: missing column(s) {missing}; "
                    f"expected {RECORDING_CSV_HEADER}"
                )
            for row in reader:
                try:
                    host_ts = float(row[fields["host_timestamp"]])
                    device_ts = int(float(row[fields["device_timestamp_us"]]))
                    sequence = int(float(row[fields["sequence"]]))
                    value = float(row[fields["value"]])
                except (KeyError, TypeError, ValueError):
                    continue
                samples.append(
                    RecordedSample(
                        host_timestamp=host_ts,
                        packet=SamplePacket(
                            protocol_id=PROTOCOL_ID,
                            sequence=sequence & 0xFFFFFFFF,
                            device_timestamp_us=device_ts & 0xFFFFFFFF,
                            value=value,
                        ),
                    )
                )
    except OSError as exc:
        raise RecordingError(f"Could not read {file_path}: {exc}") from exc

    if not samples:
        raise RecordingError(f"No samples in recording: {file_path}")
    return samples


def recording_relative_times(
    samples: Sequence[RecordedSample],
) -> list[tuple[float, SamplePacket]]:
    """
    Convert absolute host timestamps to offsets from the first sample (seconds).

    Falls back to device_timestamp_us deltas (µs → s) if host timestamps are
    missing or non-increasing.
    """
    if not samples:
        return []

    use_host = True
    prev = samples[0].host_timestamp
    for s in samples[1:]:
        if s.host_timestamp < prev - 1e-9:
            use_host = False
            break
        prev = s.host_timestamp

    out: list[tuple[float, SamplePacket]] = []
    if use_host:
        t0 = samples[0].host_timestamp
        for s in samples:
            out.append((max(0.0, s.host_timestamp - t0), s.packet))
        return out

    # Device clock path (µs, may wrap).
    t0_us = samples[0].packet.device_timestamp_us
    for s in samples:
        delta_us = (s.packet.device_timestamp_us - t0_us) & 0xFFFFFFFF
        out.append((delta_us / 1_000_000.0, s.packet))
    return out


class SensorReplay:
    """
    Background player for a CSV recording — same surface as :class:`UdpSensorReceiver`.

    Use to drive the controller Sensor path without a live ESP32. Timing follows
    host_timestamp deltas from the file (or device timestamps as fallback),
    scaled by ``speed`` (2.0 = twice as fast).
    """

    def __init__(
        self,
        samples: Sequence[RecordedSample] | Sequence[tuple[float, SamplePacket]],
        *,
        loop: bool = False,
        speed: float = 1.0,
        on_sample: SampleCallback | None = None,
        on_error: ErrorCallback | None = None,
        on_finished: Callable[[], None] | None = None,
        label: str = "recording",
    ) -> None:
        if not samples:
            raise RecordingError("Replay requires at least one sample.")

        first = samples[0]
        if isinstance(first, RecordedSample):
            self._timeline = recording_relative_times(
                samples  # type: ignore[arg-type]
            )
        else:
            self._timeline = [
                (max(0.0, float(t)), pkt)  # type: ignore[misc]
                for t, pkt in samples  # type: ignore[misc]
            ]

        self.loop = bool(loop)
        self.speed = max(0.01, float(speed))
        self.on_sample = on_sample
        self.on_error = on_error
        self.on_finished = on_finished
        self.label = label

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: SamplePacket | None = None
        self._stats = StreamStats()
        self._running = False
        self._loops_completed = 0

    @property
    def sample_count(self) -> int:
        return len(self._timeline)

    @property
    def duration_sec(self) -> float:
        if not self._timeline:
            return 0.0
        return self._timeline[-1][0]

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
            return StreamStats(
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

    @property
    def loops_completed(self) -> int:
        with self._lock:
            return self._loops_completed

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="sensor-replay",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _publish(self, packet: SamplePacket, mono: float) -> None:
        with self._lock:
            self._stats.note_packet(packet, mono)
            self._latest = packet
        if self.on_sample is not None:
            try:
                self.on_sample(packet)
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        try:
            speed = self.speed
            while not self._stop.is_set():
                origin = time.monotonic()
                for rel_t, packet in self._timeline:
                    if self._stop.is_set():
                        break
                    target = origin + (rel_t / speed)
                    while not self._stop.is_set():
                        now = time.monotonic()
                        remaining = target - now
                        if remaining <= 0:
                            break
                        # Cap sleep chunks so stop() stays responsive.
                        time.sleep(min(remaining, 0.05))
                    if self._stop.is_set():
                        break
                    self._publish(packet, time.monotonic())

                if self._stop.is_set():
                    break
                with self._lock:
                    self._loops_completed += 1
                if not self.loop:
                    break
        except Exception as exc:  # noqa: BLE001
            if self.on_error is not None and not self._stop.is_set():
                try:
                    self.on_error(str(exc))
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._running = False
            if self.on_finished is not None and not self._stop.is_set():
                try:
                    self.on_finished()
                except Exception:  # noqa: BLE001
                    pass
