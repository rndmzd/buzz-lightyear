# ESP32-S3-Zero MPU6050 wrist-wave output

This firmware reads one MPU6050 gyroscope axis and streams a normalized motion
value to a host computer over **UDP/Wi-Fi** at ~200 Hz. Each directional
half-stroke produces a positive peak; the value returns toward zero at a
direction reversal. Direction itself is discarded.

USB Serial remains available for status and optional per-sample debug output.

**Host use:** the controller GUI **Sensor** tab (`controller_gui.py`) receives
this stream and maps 0–1 samples to continuous Lovense intensity. For packet
diagnostics without Lovense, use `tools/udp_receiver.py`. Architecture and
deploy notes: **[README.md](README.md)**.

Printable / CAD enclosure files live under **`hardware/`**
(`wrist_sensor_case.stl`, `wrist_sensor_lid.stl`, FreeCAD / 3MF sources).

## Why UDP?

UDP is used for **lowest latency** and a simple fire-and-forget path:

- No connection handshake or retransmission stalls in the 200 Hz loop.
- One small datagram per sample; the host always prefers the newest data.
- Packet loss is expected on Wi-Fi; **sequence numbers** make drops visible.

TCP would add head-of-line blocking and retransmit delay, which is worse for
real-time motion than occasionally missing a sample.

## Wiring

| ESP32-S3-Zero | MPU6050 / GY-521 |
|---|---|
| 3V3 | VCC |
| GND | GND |
| GPIO8 | SDA |
| GPIO9 | SCL |

Leave `AD0` low for address `0x68`, or connect it high for `0x69`. The firmware
detects either address. Powering the module from 3.3 V avoids depending on the
details of a particular breakout board's regulator and pull-ups.

## Wi-Fi configuration (setup mode)

Station credentials (home/office SSID, password, UDP host/port) are stored in
**NVS flash** via a SoftAP setup portal. They are **not** hard-coded in source.

### Entering setup mode

| Condition | Behavior |
|-----------|----------|
| **No credentials in flash** | Setup mode starts automatically on boot |
| **Double RST** | Press the board **RESET** button **twice within 3 seconds** |

While in setup mode the onboard **WS2812 LED blinks once per second** (short
blue flash). Serial prints the SoftAP name and portal URL.

### Configuring from a host computer

1. Join the SoftAP Wi-Fi network (default open SSID **`BuzzLightyear-Setup`**).
2. Open **http://192.168.4.1/** in a browser (many OSes open a captive portal).
3. Enter:
   - Wi-Fi **SSID** and **password** the ESP32 should join for normal use
   - **Host computer LAN IPv4** (UDP destination — not `127.0.0.1`)
   - **UDP port** (default `5005`)
4. Click **Save and reboot**.
5. Reconnect the host PC to your normal Wi-Fi, then start the UDP receiver.

Credentials persist across power cycles. To change them later, use double-RST.

### Optional compile-time defaults

Copy the example board config if you want to change SoftAP name, double-reset
window, or LED pin:

```sh
# Windows
copy include\wifi_config.example.h include\wifi_config.h

# Unix
cp include/wifi_config.example.h include/wifi_config.h
```

| Constant | Meaning |
|----------|---------|
| `SETUP_AP_SSID` | SoftAP name in setup mode |
| `SETUP_AP_PASSWORD` | SoftAP password (`""` = open) |
| `DOUBLE_RESET_WINDOW_MS` | Second RST window (default 3000) |
| `STATUS_LED_PIN` | WS2812 data pin (21 on ESP32-S3-Zero) |
| `DEFAULT_UDP_HOST_IP` / `DEFAULT_UDP_PORT` | Form defaults before first save |
| `WIFI_RECONNECT_INTERVAL_MS` | Non-blocking station reconnect spacing |
| `SERIAL_STATUS_INTERVAL_MS` | Periodic Serial stats (0 = off) |
| `SERIAL_SAMPLE_OUTPUT` | `1` to print every sample on USB Serial |

After setup, put the ESP32 and host on the **same LAN** (same SSID / subnet).

### Finding the host IPv4 address

- **Windows:** `ipconfig` → look for **IPv4 Address** on the active adapter
  (e.g. `192.168.1.42`).
- **Linux:** `ip addr` or `hostname -I`.
- **macOS:** `ipconfig getifaddr en0` (or System Settings → Network).

Use that address in the setup form as the UDP host. Do **not** use
`127.0.0.1` — that is local to the ESP32 itself, not your PC.

### Host firewall

Allow inbound **UDP** on the chosen port (default **5005**) for the receiver
process, or temporarily allow Python through the firewall while testing.

- **Windows:** Windows Defender Firewall → allow app, or create an inbound rule
  for UDP port 5005.
- **Linux:** e.g. `sudo ufw allow 5005/udp` if ufw is active.
- **macOS:** allow incoming for Terminal/Python when prompted, or add a rule
  in Firewall Options.

## Packet format

Each datagram is exactly **16 bytes**, **little-endian**:

| Offset | Size | Type | Field |
|-------:|-----:|------|-------|
| 0 | 4 | `uint32` | `protocol_id` = `0x425A5531` (“BZU1”) |
| 4 | 4 | `uint32` | `sequence` (increments per successful sample) |
| 8 | 4 | `uint32` | `timestamp_us` (`micros()` on the ESP32) |
| 12 | 4 | `float` | normalized value **0.0–1.0** (IEEE-754) |

Fields are serialized explicitly as little-endian bytes (not a raw C++ struct
dump). Failed sensor reads are **not** sent as valid packets.

Expected rate: **≈200 packets/second** when Wi-Fi is connected and the sensor
is healthy. UDP can drop or reorder packets; sequence gaps on the host reveal
losses.

## Build and upload

This is a PlatformIO Arduino project. The Waveshare-recommended PlatformIO board
target is `esp32-s3-devkitm-1`, already configured in `platformio.ini`.

1. Optionally copy `include/wifi_config.example.h` → `include/wifi_config.h`
   if you need non-default SoftAP settings.
2. Connect the board by USB-C.
3. Build and flash:

   ```sh
   pio run -t upload
   ```

4. Open a serial monitor at 115200 baud:

   ```sh
   pio device monitor
   ```

5. On first boot (or after double-RST), complete **setup mode** (SoftAP + web
   form). Keep the MPU6050 still during the two-second gyro calibration that
   follows a normal (station) boot.

If upload does not start, hold **BOOT**, press and release **RESET**, then
release **BOOT** and try again.

### Serial diagnostics (not per-sample by default)

**Setup mode:**

```text
=== SETUP MODE ===
  SSID: BuzzLightyear-Setup
  Portal: http://192.168.4.1/
Onboard LED blinks once per second while in setup.
```

**Normal mode:**

```text
Loaded SSID="…" UDP 192.168.1.42:5005
Wi-Fi: connecting to SSID "…"…
Wi-Fi: connected, IP 192.168.1.55
UDP destination 192.168.1.42:5005
Sensor ready — streaming UDP samples when Wi-Fi is up
status: wifi=up ip=192.168.1.55 sent+1000 send_fail+0 sensor_fail+0 seq=1000
```

Per-sample Serial output is off by default (`SERIAL_SAMPLE_OUTPUT 0`) so USB
logging does not steal time from the 200 Hz loop. Set it to `1` only for
debugging.

## Host receiver

### Lovense control (GUI)

With the controller machine already configured (token + pairing identity — see
[README.md](README.md)):

```sh
uv run python controller_gui.py
# open the Sensor tab → stream starts on UDP 5005 by default
# Begin control → maps live value to Vibrate:0–20 (rate-limited)
```

Shared codec and background listener: `sensor_stream.py`
(`UdpSensorReceiver`, `map_value_to_level`). Optional env knobs:
`UDP_SENSOR_PORT`, `SENSOR_MAX_LEVEL`, `SENSOR_CMD_HZ`, `SENSOR_DEADBAND`,
`SENSOR_TIME_SEC`.

### Diagnostics CLI

Uses project deps only (`sensor_stream`); no extra packages beyond `uv sync`:

```sh
# Live stream (bind all interfaces, port 5005)
uv run python tools/udp_receiver.py --port 5005

# Quiet mode + CSV log (batched flush)
uv run python tools/udp_receiver.py --port 5005 --quiet --csv samples.csv

# Codec / gap-detection self-test (no hardware)
uv run python tools/udp_receiver.py --self-test
```

The CLI receiver:

- Rejects wrong size or protocol id
- Prints sequence, device timestamp, and value
- Counts sequence gaps and reorders
- Estimates packet rate and one-way stream jitter from device timestamps
- Handles Ctrl+C cleanly

CSV columns: `host_timestamp`, `device_timestamp_us`, `sequence`, `value`.

## Sensor tuning

All motion settings are near the top of `src/main.cpp`:

- `kWaveAxis`: change `Z` to `X` or `Y` to match the physical mounting.
- `kDeadbandDps`: raise it if stationary noise creates output; lower it to sense
  gentler motion.
- `kFullOutputDps`: angular speed that maps to `1.0`. Lower it for stronger peaks.
- `kFilterCutoffHz`: lower it for smoother output or raise it for faster response.
- `kResponseGamma`: use less than `1.0` to emphasize slow motion, or greater than
  `1.0` to emphasize fast motion.

Filtering is applied while the gyro signal is still signed. The firmware takes
the absolute value afterward, which preserves a distinct dip at each reversal.

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Always in setup / LED blinking | Complete the portal form, or double-RST was used; save credentials |
| SoftAP not visible | Wait for “SETUP MODE” on Serial; 2.4 GHz client; SSID `BuzzLightyear-Setup` |
| Portal will not open | Join SoftAP, then browse to `http://192.168.4.1/` |
| No station Wi-Fi connect | Re-run setup (double-RST); check SSID/password and 2.4 GHz AP |
| Serial shows IP but host gets nothing | Host firewall UDP port; setup form host IP is the PC’s LAN IP; same subnet |
| Receiver rejects packets | Port mismatch; wrong firmware; non-16-byte noise on the port |
| GUI Sensor tab idle / waiting | ESP32 on station Wi-Fi; host port matches setup form; firewall allows UDP |
| Lovense not connected in Sensor tab | Connection tab: Fetch identity + `LOVENSE_TOKEN`; owner must be paired |
| Rate ≪ 200 Hz / large gaps | RF interference, AP load, host CPU load, antivirus scanning |
| Wi-Fi drops | Firmware keeps sampling and reconnects periodically without freezing I2C |
| `MPU6050 not found` | Wiring SDA/SCL/3V3/GND; address 0x68/0x69; power the module at 3.3 V |
| Values stuck at 0 | Motion axis (`kWaveAxis`); deadband too high; keep still only during calib |

## Timing notes

- Sample period remains **5 ms** (200 Hz).
- Wi-Fi reconnect uses `millis()` interval checks — no `delay()` in the sample
  path for network recovery.
- Disconnected Wi-Fi: sampling continues; old samples are **not** queued; only
  the current sample is sent when the link is up again.
