# Buzz Lightyear

Voice (and future) triggers that drive a **Lovense** toy over the
**Standard Socket API**, with pairing handled by a separate remote web app.

## Three machines

```
┌──────────────────────────┐     HTTPS      ┌─────────────────────────────┐
│ Owner's computer         │───────────────►│ Remote pairing server       │
│ (browser: view QR)       │                │ python -m webapp            │
└──────────────────────────┘                │ · LOVENSE_TOKEN (secret)    │
                                            │ · CONTROLLER_API_KEY        │
┌──────────────────────────┐   scan QR      │ · only job: owner pairing   │
│ Owner's phone            │───────────────►│                             │
│ Lovense Connect          │                └──────────────▲──────────────┘
└──────────────────────────┘                               │
                                                           │ HTTPS + API key
                                                           │ GET /api/controller/identity
                                                           │ → { uid, platform, uname, paired }
                                                           │ (never the developer token)
┌──────────────────────────┐                               │
│ Controller computer      │───────────────────────────────┘
│ voice_trigger / etc.     │
│ · LOVENSE_TOKEN (local)  │──────── Socket.IO commands ──► Lovense cloud
│ · PAIRING_SERVER_URL     │
│ · CONTROLLER_API_KEY     │
└──────────────────────────┘
```

| Machine | Role |
|---------|------|
| **Remote server** | Pairing helper only. Owner opens site, clicks Pair, scans QR. |
| **Owner phone + PC** | PC shows QR; phone runs Lovense Connect and scans. |
| **Controller PC** | Runs voice (or other) triggers; talks to Lovense Socket API. |

Pairing binds a **`uid`** to the owner’s Connect app. Controllers do **not**
scrape the server `.env`. They call a small authenticated API to learn
`uid` / `platform`, and keep their **own** copy of `LOVENSE_TOKEN`.

## What each side needs

### Pairing server

| Variable | Purpose |
|----------|---------|
| `LOVENSE_TOKEN` | Developer token (never exposed to browsers or controller API) |
| `LOVENSE_PLATFORM` | Dashboard website name |
| `CONTROLLER_API_KEY` | Shared secret for remote controllers |

```sh
pip install -r requirements.txt
cp .env.example .env   # fill server section
python -m webapp       # e.g. http://0.0.0.0:8080/
```

Owner: open the site → **Pair with Lovense** → scan with **Lovense Connect**.

### Controller computer

| Variable | Purpose |
|----------|---------|
| `LOVENSE_TOKEN` | **Same** developer token, configured locally (not downloaded) |
| `PAIRING_SERVER_URL` | Base URL of the pairing helper |
| `CONTROLLER_API_KEY` | Same secret as on the pairing server |

On startup the controller calls:

```http
GET {PAIRING_SERVER_URL}/api/controller/identity
Authorization: Bearer {CONTROLLER_API_KEY}
```

Response (example):

```json
{
  "uid": "buzz-a1b2c3d4",
  "platform": "Rys Circus",
  "uname": "BuzzLightyear",
  "paired": true,
  "paired_at": "…",
  "token_included": false
}
```

Then the controller does its own Socket.IO flow:

1. `getToken` (local token + fetched uid)  
2. `getSocketUrl` (platform + authToken)  
3. Emit `basicapi_send_toy_command_ts` on phrase match  

```sh
# controller .env: LOVENSE_TOKEN, PAIRING_SERVER_URL, CONTROLLER_API_KEY
python controller_gui.py          # GUI: connect + fetch identity
python voice_trigger.py --model ./vosk-model-small-en-us-0.15
python voice_trigger.py --model ./vosk-model-small-en-us-0.15 --test
```

### Controller GUI

```sh
python controller_gui.py
```

Tabs:

| Tab | What it does |
|-----|----------------|
| **Connection** | Pairing server URL, API key, developer token; Test / Fetch identity / Save |
| **Triggers** | Edit phrase groups → Lovense `action` / `timeSec` / `cooldown`; load/save `triggers.json` |
| **Voice** | Vosk model + mic; Start/Stop listening; test mode; activity log |

Voice listening uses the same `TriggerEngine` + Socket.IO path as the CLI.
Save triggers from the GUI to update `triggers.json` (also used by
`voice_trigger.py`).

You do **not** manually copy `LOVENSE_UID` if `PAIRING_SERVER_URL` is set—unless
you want to override with a local `LOVENSE_UID` and leave the server URL unset.

## Layout

| Path | Role |
|------|------|
| `webapp/` | Owner pairing UI + controller identity API |
| `pairing.py` | Pairing sessions + `controller_identity()` |
| `controller_client.py` | Controller fetch of uid/platform |
| `controller_gui.py` | Controller desktop UI (server link + identity) |
| `actions.py` | Lovense Socket.IO client (commands) |
| `voice_trigger.py` | Voice controller CLI |
| `recognition.py` / `triggers.py` | STT + phrase map |
| `src/main.cpp` | ESP32 motion firmware (MPU6050 → UDP stream) |
| `include/wifi_config.example.h` | SoftAP / board defaults template (copy to `wifi_config.h`) |
| `src/setup_mode.cpp` | Double-RST SoftAP setup portal + status LED |
| `src/credentials_store.cpp` | NVS storage for station Wi-Fi + UDP target |
| `tools/udp_receiver.py` | Host UDP receiver, stats, optional CSV log |

## API (pairing server)

| Route | Who | Purpose |
|-------|-----|---------|
| `GET /` | Owner browser | Pair button + QR |
| `POST /api/pairing/start` | Owner browser | Start QR session |
| `GET /api/pairing/status` | Owner browser | Poll until paired |
| `GET /api/controller/identity` | Controllers | **Bearer / X-Api-Key** → uid, platform, paired |
| `GET /api/health` | Ops | Liveness (no secrets) |

## Triggers

Edit `triggers.json` on the **controller** (phrase → Function). See file for
examples. On match, emit Function fields via Socket.IO (not HTTP POST).

## Security notes

* Put the pairing site behind **HTTPS** on the public internet.  
* `CONTROLLER_API_KEY` should be long and random; treat it like a password.  
* Developer token stays on servers/controllers you control—never in the QR page JS.  
* The controller identity API intentionally **does not** return `LOVENSE_TOKEN`.  

## ESP32 motion (UDP over Wi-Fi)

Optional wrist-wave firmware streams a **0.0–1.0** normalized motion value from
an MPU6050 to a host PC at **≈200 packets/s** over **UDP** (low latency,
fire-and-forget; sequence numbers reveal drops). Not wired into the Lovense
controller path yet.

Full wiring, packet layout, firewall notes, and troubleshooting:
**[README-pio.md](README-pio.md)**.

```sh
# 1. Flash firmware
pio run -t upload
pio device monitor   # 115200

# 2. Setup mode (first boot, or press RST twice within 3s):
#    join SoftAP "BuzzLightyear-Setup" → http://192.168.4.1/
#    enter home Wi-Fi SSID/password + host LAN IP + UDP port → Save

# 3. On the host (same LAN as the ESP32 station), receive samples
python tools/udp_receiver.py --port 5005
python tools/udp_receiver.py --port 5005 --csv samples.csv
python tools/udp_receiver.py --self-test
```
