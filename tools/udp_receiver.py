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
import sys
import time
from pathlib import Path
from typing import TextIO

# Project root on path when launched as tools/udp_receiver.py
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sensor_stream import (  # noqa: E402
    PACKET_SIZE,
    PROTOCOL_ID,
    RECV_BUFFER_BYTES,
    PacketError,
    SamplePacket,
    StreamStats,
    decode_packet,
    encode_packet,
)

CSV_FLUSH_EVERY = 50


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

    bad_proto = encode_packet(2, 0, 0.0, protocol_id=0xDEADBEEF)
    try:
        decode_packet(bad_proto)
        check("reject bad protocol", False)
    except PacketError:
        check("reject bad protocol", True)

    stats = StreamStats()
    t0 = time.monotonic()
    stats.note_packet(decode_packet(encode_packet(10, 1000, 0.1)), t0)
    stats.note_packet(decode_packet(encode_packet(11, 6000, 0.2)), t0 + 0.005)
    stats.note_packet(decode_packet(encode_packet(15, 26000, 0.3)), t0 + 0.02)
    check("gap count (10→11→15)", stats.gaps == 3, f"gaps={stats.gaps}")
    stats.note_packet(decode_packet(encode_packet(14, 27000, 0.4)), t0 + 0.025)
    check("reorder count", stats.reordered == 1, f"reordered={stats.reordered}")
    check("received count", stats.received == 4, f"received={stats.received}")

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
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        except OSError as exc:
            print(f"warning: could not set SO_RCVBUF: {exc}", file=sys.stderr)

        sock.bind((host, port))
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
