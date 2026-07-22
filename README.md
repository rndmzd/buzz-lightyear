# Buzz Lightyear

**Voice** and **wrist-sensor** triggers that drive a **Lovense** toy over the
**Standard Socket API**, with pairing handled by a separate remote web app.

| Trigger source | How it runs |
|----------------|-------------|
| **Voice** | Vosk offline STT → phrase match in `triggers.json` → Function command |
| **Sensor** | ESP32 + MPU6050 streams 0–1 motion over UDP → continuous `Vibrate:0–20` |

## Three machines (+ optional wearable)

```
┌──────────────────────────┐     HTTPS      ┌─────────────────────────────┐
│ Owner's computer         │───────────────►│ Remote pairing server       │
│ (browser: view QR)       │                │ gunicorn + Caddy/nginx      │
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
│ controller_gui / voice   │
│ · LOVENSE_TOKEN (local)  │──────── Socket.IO commands ──► Lovense cloud
│ · PAIRING_SERVER_URL     │
│ · CONTROLLER_API_KEY     │
└────────────▲─────────────┘
             │ UDP ~200 Hz (LAN)
┌────────────┴─────────────┐
│ ESP32-S3 wrist sensor    │  (optional)
│ MPU6050 → normalized 0–1 │
└──────────────────────────┘
```

| Machine | Role |
|---------|------|
| **Remote server** | Pairing helper only. Owner opens site, clicks Pair, scans QR. |
| **Owner phone + PC** | PC shows QR; phone runs Lovense Connect and scans. |
| **Controller PC** | Runs GUI and/or voice CLI; talks to Lovense Socket API. |
| **ESP32 wrist sensor** | Optional. Streams motion over UDP for the GUI **Sensor** tab. |

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

For a quick local check (not public HTTPS):

```sh
uv sync
cp .env.example .env   # fill server section above
uv run python -m webapp       # e.g. http://0.0.0.0:8080/
```

For always-on public hosting, see **[Deploying the pairing server](#deploying-the-pairing-server)** below.

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
3. Emit `basicapi_send_toy_command_ts` on phrase match or sensor intensity  

```sh
# controller .env: LOVENSE_TOKEN, PAIRING_SERVER_URL, CONTROLLER_API_KEY
uv sync   # includes voice deps (vosk / sounddevice) via the controller group
uv run python controller_gui.py          # GUI: identity, voice, sensor
uv run python voice_trigger.py --model ./vosk-model-small-en-us-0.15
uv run python voice_trigger.py --model ./vosk-model-small-en-us-0.15 --test
```

### Controller GUI

```sh
uv run python controller_gui.py
```

Tabs:

| Tab | What it does |
|-----|----------------|
| **Connection** | Pairing server URL, API key, developer token; Test / Fetch identity / Save |
| **Triggers** | Edit phrase groups → Lovense `action` / `timeSec` / `cooldown`; load/save `triggers.json` |
| **Voice** | Vosk model + mic; Start/Stop listening; test mode; activity log |
| **Sensor** | UDP motion from ESP32 → continuous `Vibrate:0–20` (or test-mode log only) |

Voice listening uses the same `TriggerEngine` + Socket.IO path as the CLI.
Save triggers from the GUI to update `triggers.json` (also used by
`voice_trigger.py`).

#### Sensor tab (ESP32 → Lovense)

Selecting the **Sensor** tab (or **Start stream + Lovense**) binds the UDP
listen port and connects the Socket API when identity + token are ready.
**Begin control** maps the live 0–1 stream to intensity; **End control**
sends `Stop`. Commands use `stopPrevious=1` so each update replaces the
previous Function instead of stacking.

| Setting | Env (optional) | Default | Purpose |
|---------|----------------|---------|---------|
| UDP listen port | `UDP_SENSOR_PORT` | `5005` | Must match the ESP32 setup form |
| Max vibration level | `SENSOR_MAX_LEVEL` | `20` | Sensor `1.0` → `Vibrate:N` |
| Command rate (Hz) | `SENSOR_CMD_HZ` | `10` | How often intensity is pushed (not 200 Hz) |
| Input deadband | `SENSOR_DEADBAND` | `0.02` | Values ≤ this map to level 0 / Stop |
| Command timeSec | `SENSOR_TIME_SEC` | `1.0` | Duration on each Function emit |
| Test mode | (GUI only) | off | Map + log only; no Socket emit |

Host-side packet codec and receiver: `sensor_stream.py`. Firmware, wiring,
and SoftAP setup: **[README-pio.md](README-pio.md)**. Printable enclosure:
`hardware/`.

**Record / replay:** while live UDP is running, **Start recording** writes
samples to a CSV (`host_timestamp`, `device_timestamp_us`, `sequence`,
`value` — same schema as `tools/udp_receiver.py --csv`). Later, **Play
recording** streams that file with original timing (optional loop / speed)
so you can map motion to Lovense without the ESP32 online. CLI:

```sh
uv run python tools/udp_receiver.py --port 5005 --csv capture.csv
uv run python tools/udp_receiver.py --replay capture.csv --speed 1 --loop
```

You do **not** manually copy `LOVENSE_UID` if `PAIRING_SERVER_URL` is set—unless
you want a local-only setup: set `LOVENSE_UID` / `LOVENSE_PLATFORM` and leave
`PAIRING_SERVER_URL` unset.

## Layout

| Path | Role |
|------|------|
| `webapp/` | Owner pairing UI + controller identity API |
| `pairing.py` | Pairing sessions + `controller_identity()` |
| `controller_client.py` | Controller fetch of uid/platform (+ health) |
| `controller_gui.py` | Controller desktop UI (identity, triggers, voice, sensor) |
| `actions.py` | Lovense Socket.IO client (Function + intensity + QR) |
| `voice_trigger.py` | Voice controller CLI |
| `recognition.py` / `triggers.py` | STT + phrase map / `TriggerEngine` |
| `triggers.json` | Phrase → Lovense Function mappings (controller) |
| `config.py` | Shared paths, defaults, `.env` helpers |
| `deploy/caddy/` | Docker Compose + Caddy reverse proxy for pairing |
| `deploy/docker/Dockerfile` | Pairing server container image (gunicorn) |
| `deploy/systemd/buzz-pairing.service` | Host systemd unit (gunicorn + restart) |
| `deploy/nginx/pairing-server.conf` | Host nginx TLS reverse proxy sample |
| `webapp/wsgi.py` | Production WSGI entry (`webapp.wsgi:app`) |
| `src/main.cpp` | ESP32 motion firmware (MPU6050 → UDP stream) |
| `include/wifi_config.example.h` | SoftAP / board defaults template (copy to `wifi_config.h`) |
| `src/setup_mode.cpp` | Double-RST SoftAP setup portal + status LED |
| `src/credentials_store.cpp` | NVS storage for station Wi-Fi + UDP target |
| `sensor_stream.py` | UDP packet codec, stats, `UdpSensorReceiver`, level mapping |
| `tools/udp_receiver.py` | CLI host UDP receiver, stats, optional CSV log |
| `hardware/` | Wrist sensor enclosure (FreeCAD + STL/3MF) |
| `platformio.ini` | PlatformIO env for ESP32-S3-Zero |
| `pyproject.toml` / `uv.lock` | Python deps (`uv sync`; controller group = vosk/mic) |

## API (pairing server)

| Route | Who | Purpose |
|-------|-----|---------|
| `GET /` | Owner browser | Pair button + QR |
| `POST /api/pairing/start` | Owner browser | Start QR session |
| `GET /api/pairing/status` | Owner browser | Poll until paired |
| `POST /api/pairing/reset` | Owner browser | Drop active pairing socket session |
| `GET /api/controller/identity` | Controllers | **Bearer / X-Api-Key** → uid, platform, paired |
| `GET /api/health` | Ops | Liveness (no secrets) |

## Triggers

Edit `triggers.json` on the **controller** (phrase → Function). See file for
examples. On match, emit Function fields via Socket.IO (not HTTP POST).

## Deploying the pairing server

The pairing helper is the only process that should be reachable on the public
internet. Controllers stay on private machines and call
`GET /api/controller/identity` over HTTPS with `CONTROLLER_API_KEY`.

| Option | When to use | Files |
|--------|-------------|--------|
| **A. Caddy + Docker** | VPS/home server; want auto HTTPS and restarts | [deploy/caddy/](deploy/caddy/), [deploy/docker/Dockerfile](deploy/docker/Dockerfile) |
| **B. nginx + systemd** | Linux host where you already run nginx | [deploy/nginx/pairing-server.conf](deploy/nginx/pairing-server.conf), [deploy/systemd/buzz-pairing.service](deploy/systemd/buzz-pairing.service) |
| **Local debug only** | Laptop / one-shot pairing | `uv run python -m webapp` |

**Requirements that apply to every production install**

* Public hostname (e.g. `pair.example.com`) with DNS **A/AAAA** pointing at the host.
* **HTTPS** in front of the app (Caddy or nginx). Do not expose Flask/gunicorn on `:8080` to the world.
* `.env` with at least:

  | Variable | Notes |
  |----------|--------|
  | `LOVENSE_TOKEN` | Developer token (server-side only) |
  | `LOVENSE_PLATFORM` | Name from the Lovense developer dashboard |
  | `CONTROLLER_API_KEY` | Long random secret; same value on every controller |

* Production app entry is **gunicorn** with **exactly one worker** (pairing sessions are in-memory):

  ```sh
  gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 8 webapp.wsgi:app
  ```

  Docker and the systemd unit already use this. Do not raise `--workers` above `1`.

* After deploy, set each controller’s  
  `PAIRING_SERVER_URL=https://pair.example.com`  
  (no trailing slash) and the same `CONTROLLER_API_KEY` / `LOVENSE_TOKEN`.

---

### Option A — Caddy + Docker (recommended)

Stack: **Caddy** (TLS + reverse proxy) and **pairing** (gunicorn image). Both use
`restart: unless-stopped`. Pair state lives in a Docker volume.

**1. Prepare the host**

* Install [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2.
* Open inbound **TCP 80** and **443** (and UDP 443 if you want HTTP/3).
* Point DNS for your domain at this machine.

**2. Configure secrets**

```sh
cd /path/to/buzz-lightyear
cp .env.example .env
```

Edit `.env` (pairing section):

```env
LOVENSE_TOKEN=…
LOVENSE_PLATFORM=Rys Circus
CONTROLLER_API_KEY=…          # long random string
PAIRING_DOMAIN=pair.example.com
```

`PAIRING_DOMAIN` is read by Compose for Caddy’s site address. Compose also
passes the Lovense variables into the `pairing` container.

**3. Start the stack**

```sh
docker compose -f deploy/caddy/compose.yml up -d --build
docker compose -f deploy/caddy/compose.yml ps
docker compose -f deploy/caddy/compose.yml logs -f
```

Caddy obtains a Let’s Encrypt certificate automatically when `PAIRING_DOMAIN`
is a real public name. For a local smoke test only:

```sh
PAIRING_DOMAIN=localhost docker compose -f deploy/caddy/compose.yml up --build
```

**4. Verify**

```sh
curl -sS https://pair.example.com/api/health
# → {"ok":true,"role":"pairing_helper",…}

# Owner UI
# open https://pair.example.com/ → Pair with Lovense → scan with Connect
```

**5. Day-2 operations**

```sh
# Update code + rebuild
git pull
docker compose -f deploy/caddy/compose.yml up -d --build

# Logs
docker compose -f deploy/caddy/compose.yml logs -f pairing
docker compose -f deploy/caddy/compose.yml logs -f caddy

# Stop
docker compose -f deploy/caddy/compose.yml down
# Keep volumes (pair state + certs): omit -v
# Wipe pair state + certs:  docker compose -f deploy/caddy/compose.yml down -v
```

| Volume | Contents |
|--------|----------|
| `pairing_data` | `PAIRING_STATE_PATH=/data/pairing_state.json` (uid / paired status) |
| `caddy_data` | ACME certificates |
| `caddy_config` | Caddy internal config |

---

### Option B — nginx + systemd (host install)

Runs gunicorn under **systemd** on loopback; **nginx** terminates TLS on 80/443.
Default install path in the unit file is `/opt/buzz-lightyear` — edit the unit
if your checkout lives elsewhere.

**1. Install the app**

```sh
# Example layout; use your own user/path if preferred
sudo mkdir -p /opt/buzz-lightyear
sudo chown "$USER":"$USER" /opt/buzz-lightyear
cd /opt/buzz-lightyear
# clone or copy the repo into this directory, then:
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is not installed
uv sync --no-default-groups    # pairing deps only (skip vosk/mic); or: uv sync

cp .env.example .env
# Set LOVENSE_TOKEN, LOVENSE_PLATFORM, CONTROLLER_API_KEY
# Leave WEB_HOST unset in .env — the unit forces 127.0.0.1
```

Create a system user (matches the unit’s `User=buzz`):

```sh
sudo useradd --system --home /opt/buzz-lightyear --shell /usr/sbin/nologin buzz
sudo chown -R buzz:buzz /opt/buzz-lightyear
```

**2. Enable the systemd service**

```sh
sudo cp deploy/systemd/buzz-pairing.service /etc/systemd/system/
# If the repo is not at /opt/buzz-lightyear, edit WorkingDirectory, EnvironmentFile,
# ExecStart, and ReadWritePaths in the unit before enabling.
sudo systemctl daemon-reload
sudo systemctl enable --now buzz-pairing
sudo systemctl status buzz-pairing
journalctl -u buzz-pairing -f
```

Confirm the app answers on loopback only:

```sh
curl -sS http://127.0.0.1:8080/api/health
```

**3. Install nginx + TLS**

```sh
sudo apt install nginx certbot python3-certbot-nginx   # Debian/Ubuntu example
sudo cp deploy/nginx/pairing-server.conf /etc/nginx/sites-available/pairing-server
sudo ln -sf /etc/nginx/sites-available/pairing-server /etc/nginx/sites-enabled/
```

Edit the site file:

* Set every `server_name` to your domain (e.g. `pair.example.com`).
* Adjust `ssl_certificate` / `ssl_certificate_key` paths, **or** obtain certs first
  with certbot (see below).

Issue certificates (DNS must already point here; port 80 reachable):

```sh
# Option 1: certbot manages the nginx site
sudo certbot --nginx -d pair.example.com

# Option 2: certbot certonly, keep the sample ssl_certificate paths
# sudo certbot certonly --webroot -w /var/www/certbot -d pair.example.com
# (ensure the ACME location in the sample config matches)

sudo nginx -t && sudo systemctl reload nginx
```

**Optional HTTP basic auth** (owner UI / pairing routes; controller API stays
on API key only):

```sh
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-buzz owner
sudo chown root:www-data /etc/nginx/.htpasswd-buzz
sudo chmod 640 /etc/nginx/.htpasswd-buzz
# Uncomment the two auth_basic* lines in the HTTPS server block of
# deploy/nginx/pairing-server.conf, then:
sudo nginx -t && sudo systemctl reload nginx
```

**4. Verify**

```sh
curl -sS https://pair.example.com/api/health
# open https://pair.example.com/ in a browser and complete a pair
```

**5. Day-2 operations**

```sh
cd /opt/buzz-lightyear
sudo -u buzz git pull          # or your update process
sudo -u buzz uv sync --no-default-groups
sudo systemctl restart buzz-pairing

journalctl -u buzz-pairing -f
sudo tail -f /var/log/nginx/buzz-pairing.access.log
```

Pair state on the host is `pairing_state.json` in the project directory (or
`PAIRING_STATE_PATH` if you set it in the unit / `.env`).

**Manual production command** (same process model without systemd):

```sh
uv run gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 8 webapp.wsgi:app
```

---

### After either option

1. Open `https://<your-domain>/` as the device owner → **Pair with Lovense** → scan with **Lovense Connect**.
2. On each controller machine:

   ```env
   LOVENSE_TOKEN=…                 # same developer token as the server
   PAIRING_SERVER_URL=https://pair.example.com
   CONTROLLER_API_KEY=…            # same as server
   ```

   ```sh
   uv sync
   uv run python controller_gui.py
   # Connection tab: Test / Fetch identity
   ```

3. Confirm identity without the GUI:

   ```sh
   curl -sS -H "Authorization: Bearer $CONTROLLER_API_KEY" \
     https://pair.example.com/api/controller/identity
   ```

## Security notes

* Put the pairing site behind **HTTPS** on the public internet (see deploy options above).
* `CONTROLLER_API_KEY` should be long and random; treat it like a password.
* Developer token stays on servers/controllers you control—never in the QR page JS.
* The controller identity API intentionally **does not** return `LOVENSE_TOKEN`.
* Prefer binding the app to **127.0.0.1** and letting Caddy/nginx own ports 80/443.
* Keep **gunicorn `--workers 1`** so in-memory pairing sessions stay consistent.

## ESP32 motion (UDP over Wi-Fi)

Optional wrist-wave firmware streams a **0.0–1.0** normalized motion value from
an MPU6050 to a host PC at **≈200 packets/s** over **UDP** (low latency,
fire-and-forget; sequence numbers reveal drops).

On the controller PC, the GUI **Sensor** tab receives that stream and maps it
to continuous Lovense intensity (`Vibrate:0–20` via Socket.IO). For diagnostics
without Lovense, use the CLI receiver.

Full wiring, packet layout, SoftAP setup, firewall notes, and troubleshooting:
**[README-pio.md](README-pio.md)**. Enclosure models: `hardware/`.

```sh
# 1. Flash firmware
pio run -t upload
pio device monitor   # 115200

# 2. Setup mode (first boot, or press RST twice within 3s):
#    join SoftAP "BuzzLightyear-Setup" → http://192.168.4.1/
#    enter home Wi-Fi SSID/password + host LAN IP + UDP port → Save

# 3. On the host (same LAN as the ESP32 station)
uv run python controller_gui.py          # Sensor tab → Begin control
# or diagnostics only:
uv run python tools/udp_receiver.py --port 5005
uv run python tools/udp_receiver.py --port 5005 --csv samples.csv
uv run python tools/udp_receiver.py --self-test
```
