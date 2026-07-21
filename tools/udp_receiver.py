#!/usr/bin/env python3
"""
Receive ESP32 wrist-wave UDP samples and print live diagnostics.

Packet layout (16 bytes, little-endian)::

    offset  size  type    field
    0       4     uint32  protocol_id  = 0x425A5531 ("BZU1")
    4       4     uint32  sequence
    8       4     uint32  device_timestamp_us
    12      4     float   value (0.0–1.0)

Examples::

    python tools/udp_receiver.py --port 5005
    python tools/udp_receiver.py --port 5005 --csv samples.csv
    python tools/udp_receiver.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

# Must match firmware kProtocolId / kPacketSize.
PROTOCOL_ID = 0x425A5531
PACKET_SIZE = 16
PACKET_STRUCT = struct.Struct("<I I I f")  # protocol, seq, ts_us, value

# Large enough for a short burst of 200 Hz packets if the host stalls briefly.
RECV_BUFFER_BYTES = 256 * 1024
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
    gaps: int = 0  # missing packets inferred from sequence
    reordered: int = 0
    last_sequence: int | None = None
    # Device-timestamp deltas (µs) between consecutive accepted packets
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
                # Duplicate or out-of-order relative to last accepted seq.
                self.reordered += 1
        self.last_sequence = packet.sequence

        if self.last_device_ts is not None:
            # Nominal period is 5000 µs at 200 Hz.
            dt = float(
                (packet.device_timestamp_us - self.last_device_ts) & 0xFFFFFFFF
            )
            # Only score positive forward steps under 100 ms as inter-arrival.
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


def run_self_test() -> int:
    """Encode/decode round-trip, reject malformed, and sequence-gap checks."""
    failures = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {name}")
        else:
            failures += 1
            msg = f"  FAIL  {name}"
            if detail:
                msg += f" — {detail}"
            print(msg)

    print("Self-test: packet codec and stats")

    raw = encode_packet(1, 123456, 0.42)
    check("encode length", len(raw) == PACKET_SIZE, f"len={len(raw)}")
    pkt = decode_packet(raw)
    check("protocol id", pkt.protocol_id == PROTOCOL_ID)
    check("sequence", pkt.sequence == 1)
    check("timestamp", pkt.device_timestamp_us == 123456)
    check("value", abs(pkt.value - 0.42) < 1e-6, f"value={pkt.value}")

    # Malformed: wrong size
    try:
        decode_packet(raw[:15])
        check("reject short packet", False)
    except PacketError:
        check("reject short packet", True)

    try:
        decode_packet(raw + b"\x00")
        check("reject long packet", False)
    except PacketError:
        check("reject long packet", True)

    # Malformed: bad protocol
    bad_proto = encode_packet(2, 0, 0.0, protocol_id=0xDEADBEEF)
    try:
        decode_packet(bad_proto)
        check("reject bad protocol", False)
    except PacketError:
        check("reject bad protocol", True)

    # Sequence gap / reorder bookkeeping
    stats = StreamStats()
    t0 = time.monotonic()
    stats.note_packet(decode_packet(encode_packet(10, 1000, 0.1)), t0)
    stats.note_packet(decode_packet(encode_packet(11, 6000, 0.2)), t0 + 0.005)
    stats.note_packet(decode_packet(encode_packet(15, 26000, 0.3)), t0 + 0.02)
    check("gap count (10→11→15)", stats.gaps == 3, f"gaps={stats.gaps}")
    stats.note_packet(decode_packet(encode_packet(14, 27000, 0.4)), t0 + 0.025)
    check("reorder count", stats.reordered == 1, f"reordered={stats.reordered}")
    check("received count", stats.received == 4, f"received={stats.received}")

    # Jitter: perfect 5 ms spacing → near-zero mean error
    stats2 = StreamStats()
    for i in range(5):
        stats2.note_packet(
            decode_packet(encode_packet(i + 1, 10000 + i * 5000, 0.0)),
            t0 + i * 0.005,
        )
    check(
        "low jitter on ideal stream",
        stats2.mean_jitter_us() < 1.0,
        f"mean_jitter_us={stats2.mean_jitter_us():.3f}",
    )

    if failures:
        print(f"Self-test FAILED ({failures} check(s))")
        return 1
    print("Self-test OK")
    return 0


def open_csv(path: Path) -> tuple[TextIO, csv.writer]:
    fh = path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(fh)
    writer.writerow(
        ["host_timestamp", "device_timestamp_us", "sequence", "value"]
    )
    fh.flush()
    return fh, writer


def receive_loop(
    *,
    host: str,
    port: int,
    csv_path: Path | None,
    quiet: bool,
    status_interval: float,
) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stats = StreamStats()
    csv_fh: TextIO | None = None
    try:
        # Prefer large OS receive buffer so brief host stalls do not drop UDP.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        except OSError as exc:
            print(f"warning: could not set SO_RCVBUF: {exc}", file=sys.stderr)

        sock.bind((host, port))
        # Blocking recv — no artificial polling sleep.
        sock.settimeout(None)

        print(f"Listening on UDP {host}:{port} (Ctrl+C to stop)")
        print(
            f"Expect protocol 0x{PROTOCOL_ID:08X}, {PACKET_SIZE}-byte LE packets "
            f"@ ~200 Hz"
        )

        csv_writer: csv.writer | None = None
        csv_pending = 0

        if csv_path is not None:
            csv_fh, csv_writer = open_csv(csv_path)
            print(f"CSV logging → {csv_path}")

        last_status = time.monotonic()

        while True:
            data, addr = sock.recvfrom(2048)
            host_ts = time.time()
            mono = time.monotonic()

            try:
                packet = decode_packet(data)
            except PacketError as exc:
                stats.note_rejected()
                if not quiet:
                    print(f"reject from {addr[0]}:{addr[1]}: {exc}")
                continue

            stats.note_packet(packet, mono)

            if not quiet:
                print(
                    f"seq={packet.sequence:10d}  "
                    f"ts_us={packet.device_timestamp_us:10d}  "
                    f"value={packet.value:7.4f}  "
                    f"from={addr[0]}"
                )

            if csv_writer is not None and csv_fh is not None:
                csv_writer.writerow(
                    [
                        f"{host_ts:.6f}",
                        packet.device_timestamp_us,
                        packet.sequence,
                        f"{packet.value:.6f}",
                    ]
                )
                csv_pending += 1
                if csv_pending >= CSV_FLUSH_EVERY:
                    csv_fh.flush()
                    csv_pending = 0

            if status_interval > 0 and (mono - last_status) >= status_interval:
                rate = stats.rate_hz(mono)
                print(
                    f"[stats] rate≈{rate:.1f} Hz  recv={stats.received}  "
                    f"reject={stats.rejected}  gaps={stats.gaps}  "
                    f"reorder={stats.reordered}  "
                    f"jitter_mean={stats.mean_jitter_us():.0f} µs  "
                    f"jitter_max={stats.jitter_max_us:.0f} µs",
                    file=sys.stderr,
                )
                stats.reset_window(mono)
                last_status = mono

    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        print(
            f"Totals: recv={stats.received} reject={stats.rejected} "
            f"gaps={stats.gaps} reorder={stats.reordered}",
            file=sys.stderr,
        )
        return 0
    finally:
        if csv_fh is not None:
            try:
                csv_fh.flush()
            except OSError:
                pass
            csv_fh.close()
        sock.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Receive Buzz Lightyear ESP32 motion samples over UDP.",
    )
    p.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: all interfaces).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=5005,
        help="UDP port (must match firmware UDP_PORT).",
    )
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        metavar="PATH",
        help="Append decoded samples to this CSV file (batched flush).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print every sample; still print periodic stats.",
    )
    p.add_argument(
        "--status-interval",
        type=float,
        default=1.0,
        metavar="SEC",
        help="Seconds between stderr rate/jitter stats (0 disables).",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run encode/decode and stats checks, then exit.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return run_self_test()
    return receive_loop(
        host=args.host,
        port=args.port,
        csv_path=args.csv,
        quiet=args.quiet,
        status_interval=args.status_interval,
    )


if __name__ == "__main__":
    sys.exit(main())
