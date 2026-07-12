<p align="center">
  <img src="assets/logo.svg" width="128" alt="ReoLapse logo">
</p>

<h1 align="center">ReoLapse</h1>

<p align="center">
  Turn your Reolink cameras (or NVR) into daily, yearly, and storm timelapses —<br>
  deflickered, weather-aware, and browsable from a small built-in web UI.
</p>

<p align="center">
  <a href="https://github.com/SeriesOfTubez/reolapse/actions/workflows/security.yml"><img src="https://github.com/SeriesOfTubez/reolapse/actions/workflows/security.yml/badge.svg" alt="Security scan"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
</p>

ReoLapse polls a snapshot from each camera on an interval, builds a
deflickered **daily** video per camera, archives frames into an ever-growing
**yearly** "changing seasons" video, and — when severe weather rolls in —
captures faster and cuts a dedicated **event** clip. Everything is browsable
through a bundled single-page web app.

> **Two things to know before you run this:** the web UI has no login (see
> [Security](#security) — this is a LAN tool, don't expose it to the
> internet), and this project was built with AI assistance (see
> [AI-assisted development](#ai-assisted-development) for what that means and
> what's checked before anything ships).

---

## Features

- **Daily timelapses** per camera, with ffmpeg `deflicker` to tame
  auto-exposure flicker.
- **Yearly "seasons" timelapse.** Frames are archived hourly and kept forever;
  the yearly video is rendered from a configurable, re-tunable subset
  (e.g. 10 frames/day within daylight hours ≈ a 2-minute year).
- **Weather-aware storm bursts.** Polls NWS alerts + Open-Meteo (free, no API
  keys). During storms/snow it captures every 10s instead of every 60s, and
  the nightly build cuts a per-storm clip.
- **Lunar event detection.** Computes full/blue/harvest moons and lunar
  eclipses (blood moon = total) locally via Skyfield — no location or API
  needed.
- **Frame tagging & metadata.** Active conditions (`storm`, `snow`, `rain`,
  `full-moon`, `blue-moon`, `harvest-moon`, `blood-moon`, `lunar-eclipse`) are logged and embedded in each
  JPEG, so the data stays self-describing and searchable later.
- **PTZ-aware.** For auto-tracking cameras, frames captured away from the
  camera's home position are quarantined so they don't jerk the timelapse.
- **Direct or via NVR.** Talk to each camera directly, or pull every channel
  through one Reolink NVR with a single credential.
- **Bundled web UI.** Browse Daily / Yearly / Event videos per camera, with
  playback-speed controls and downloads. Range requests supported for seeking.
- **Runs anywhere.** A Linux VM with systemd units, or Docker Compose.
- **Secrets stay out of the repo.** Credentials live in `.env`, referenced from
  config as `${VAR}`.

## Supported hardware

Any Reolink device that exposes the HTTP **`Snap`** CGI command — which is
essentially all current cameras and NVRs. It works both ways, so use whichever
is convenient:

- **Through an NVR** (`host` = NVR IP, `channel` = the camera's channel) — one
  host and one credential for every camera. Convenient for pulling several
  cameras, and needed if they sit on the NVR's isolated PoE network.
- **Directly** to a camera (`host` = camera IP, `channel: 0`) — no NVR
  dependency, and the only way to reach a lens the NVR doesn't expose (e.g. a
  dual-lens camera's second lens).

Both return the same full-resolution main-stream snapshot on current hardware
(verified on an RLN36 as byte-for-byte identical). Some older NVRs may hand
back a reduced-resolution snapshot for a channel — if you see that, pull that
camera directly instead.

Developed and tested against a Reolink **RLN36** NVR with **TrackMix WiFi**,
**OMVI 3i**, and **Video Doorbell WiFi** cameras (HTTPS, self-signed certs).
Other models exposing the same API should work; reports welcome.

> **Note on multi-lens cameras:** an NVR exposes one feed per camera. To
> capture a second lens (e.g. a dual-lens unit's wide + tele), address that
> camera directly and add each lens as its own `channel`.

## How it works

```
                 capture.py  ── Snap API ──►  cameras / NVR
                     │
     writes JPEGs +  ▼
     conditions log  data/snapshots/<cam>/<date>/<HHMMSS>.jpg
                     │
   build_timelapse.py│  (nightly + weekly)
                     ├─►  data/videos/<cam>/daily/<date>.mp4
                     ├─►  data/videos/<cam>/events/<date>_<tag>.mp4
                     ├─►  data/yearly_frames/<cam>/<year>/…   (kept forever)
                     └─►  data/videos/<cam>/yearly/<year>.mp4
                     │
        webapp/app.py▼  serves the SPA + videos on :8080
```

Snapshots are pruned after `keep_snapshots_days`, but **only** once a day's
daily video exists — a missed build never silently loses a day. Yearly archive
frames are never pruned.

## Requirements

- Python 3.9+
- ffmpeg on `PATH`
- Reolink camera(s) and/or NVR reachable on your network
- A CPU exposing the **x86-64-v2** instruction baseline (SSE4.1, SSE4.2,
  POPCNT — needed by NumPy, which Skyfield uses for lunar event detection).
  Any real CPU from the last ~15 years has this. **Running in a VM (Proxmox,
  KVM, ESXi, etc.)?** Generic/portable virtual CPU types (e.g. Proxmox's
  default `kvm64`/`qemu64`, which reports as "Common KVM processor") often
  expose only SSE2 and will *not* meet this baseline — NumPy fails at runtime
  and lunar tagging silently stops working (everything else is unaffected).
  Set the VM's CPU type to `host` (passes through the physical CPU, best
  performance) or a synthetic type that guarantees v2+, such as
  `x86-64-v2-AES` or `x86-64-v3`, then reboot the VM. Verify with:
  `grep -o 'sse4_2\|popcnt' /proc/cpuinfo` — if that prints nothing, the
  baseline isn't met.

## Quick start (Docker)

```bash
git clone https://github.com/SeriesOfTubez/reolapse.git
cd reolapse

cp config.example.yaml config.yaml   # edit: cameras, location, options
cp .env.example .env                 # set REOLINK_PASSWORD

docker compose up -d --build
```

Open <http://localhost:8080>. Three services start: `capture` (continuous),
`scheduler` (nightly/weekly builds), and `web`. Keep `storage.root: ./data` in
`config.yaml` so data lands on the Docker volume.

## Install on a Linux VM (systemd)

> **Running this in a VM?** Make sure the hypervisor exposes at least the
> x86-64-v2 CPU baseline to the guest — see [Requirements](#requirements).
> Proxmox's default CPU type doesn't; `host` or `x86-64-v3` does.

```bash
sudo mkdir -p /opt/reolapse && sudo chown "$USER" /opt/reolapse
git clone https://github.com/SeriesOfTubez/reolapse.git /opt/reolapse
cd /opt/reolapse

sudo apt install -y ffmpeg
python3 -m venv venv && venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml   # edit for your setup
cp .env.example .env                  # set REOLINK_PASSWORD
chmod 600 .env config.yaml
```

Test one capture (`venv/bin/python capture.py -v` — a JPEG should appear under
`data/snapshots/…`), then install the services. The units in `deploy/` assume
user `ubuntu` and `/opt/reolapse` — edit `User=`/paths if yours differ:

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reolapse-capture.service \
     reolapse-web.service reolapse-daily.timer reolapse-yearly.timer
```

Set the machine's timezone (`sudo timedatectl set-timezone …`) so capture days
line up with your local midnight.

## Configuration

Everything lives in `config.yaml` (copy from `config.example.yaml`). Secrets do
not: reference them as `${VAR}` and put the values in `.env`. Highlights:

| Key | Meaning |
|---|---|
| `cameras[].host` / `channel` | Camera IP + `0`, or NVR IP + channel number |
| `cameras[].ptz_home` | Quarantine frames taken off a PTZ camera's home position |
| `capture.interval_seconds` | Base capture cadence (default 60) |
| `capture.start_time`/`end_time` | Optional daily capture window |
| `storage.keep_snapshots_days` | Retention for raw frames after their video builds |
| `daily_video.deflicker_size` | Deflicker window; `0` disables |
| `yearly.video_frames_per_day` / `video_window` | Pacing of the yearly video |
| `events.weather_enabled` | Storm/snow/rain tagging + burst capture (needs `events.zip` or `latitude`/`longitude`) |
| `events.lunar_enabled` | Moon-event tagging — no location required |
| `events_video.tags` | Which tags get their own `<date>_<tag>.mp4` clip (default `storm`, `snow`; any tag works, including moon events) |
| `events_video.deflicker_size` / `deflicker_by_tag` | Deflicker for event clips — off by default (protects lightning in storm clips), overridable per tag (e.g. enable for `snow`) |

See the inline comments in `config.example.yaml` for the full reference.

## Usage

```bash
# One-off / manual builds (the scheduler or systemd timers do these for you):
python build_timelapse.py daily                       # yesterday, all cameras
python build_timelapse.py daily --date 2026-07-04 --camera front-yard
python build_timelapse.py yearly --year 2026
python build_timelapse.py events --date 2026-07-15    # rebuild event clips

python capture.py --loop        # continuous capture (service/container does this)
python webapp/app.py            # serve the web UI
```

## Weather tagging & storm bursts

`events.weather_enabled` and `events.lunar_enabled` are independent
switches — turn on either, both, or neither.

With `events.weather_enabled: true` and a location set (`events.zip`, or
`latitude`/`longitude`), capture polls NWS + Open-Meteo every `poll_minutes`
for storm/snow/rain conditions. Active tags are appended to
`data/conditions/<date>.jsonl` and embedded in each frame as a JPEG comment
(`{"tags":["storm"]}`, visible in exiftool). Storms/snow trigger burst
capture, and the nightly build renders a clip per event span into the
**Events** tab. Deflicker for these clips is off by default (it would smooth
away lightning flashes) but is fully configurable — see
`events_video.deflicker_size` / `deflicker_by_tag` in the config table below
if you'd like it on for snow, which has no lightning to protect. If weather
tagging is enabled without a resolvable location, storm/snow tagging is
skipped and the web UI shows a warning banner.

## Lunar event detection

With `events.lunar_enabled: true`, ReoLapse computes real moon events — full
moon, blue moon, harvest moon, and lunar eclipses — using
[Skyfield](https://rhodesmill.org/skyfield/) and a local JPL ephemeris
(`de421.bsp`, ~17 MB, downloaded once into `data/ephemeris/`). **No location
is required**: a full moon happens at the same instant everywhere on Earth,
so the phase-based tags (`full-moon`, `blue-moon`, `harvest-moon`) work with
nothing else configured. This does need a CPU meeting the x86-64-v2 baseline
(see [Requirements](#requirements)) — on an under-specified VM it fails
silently, logging `event source failed: NumPy was built with baseline
optimizations...` while the rest of ReoLapse keeps working normally.

Eclipses are a little more subtle. The eclipse itself is also a geocentric
event, but *visibility* is not — only the hemisphere facing the Moon at that
moment can see it. If a location is configured (shared with the weather
settings), an eclipse is only tagged `blood-moon` (total) or `lunar-eclipse`
(partial) when the Moon was actually above your horizon for it; without a
location, every eclipse is tagged unconditionally since there's nothing to
check visibility against.

Lunar tags are metadata only by default — they don't trigger burst capture.
Add them to `events_video.tags` if you want an automatic clip, e.g.
`2026-03-03_blood-moon.mp4`.

## PTZ cameras

Add a `ptz_home` block (see `config.example.yaml`). Before each snapshot,
capture reads `GetPtzCurPos` and compares pan/tilt to the configured home;
off-home frames go to an `offposition/` subfolder — excluded from videos, still
pruned normally. Whichever axes the response includes are checked; an NVR
relays only pan (usually enough). Set `ptz_home.host` to the camera's own IP if
you need the tilt axis. The check fails open — a position-query error keeps the
frame.

## Security

- **There is no authentication.** The web UI has no login, no access control,
  nothing — anyone who can reach port 8080 can browse and download every
  video. **ReoLapse is a private, LAN-only tool. Do not port-forward it or
  otherwise expose it to the internet.** If you need remote access, put it on
  a VPN (Tailscale, WireGuard) or behind a reverse proxy that adds its own
  auth and TLS — don't rely on ReoLapse itself for either.
- Credentials live in `.env` (gitignored), never in `config.yaml`.
- Prefer a **dedicated, least-privilege** camera/NVR account for ReoLapse. The
  Snap API passes credentials as URL parameters, so avoid `&`, `#`, `%` in that
  password.
- The bundled Flask server is a dev-grade WSGI server — fine for a trusted
  LAN, not a hardened production server.

## AI-assisted development

This project was built with AI pair-programming assistance (Claude, via
Claude Code) under human direction and review — most of the code and docs,
including this README, were AI-generated. If that's a dealbreaker for you,
that's a reasonable position; here's what's in place either way so you can
judge for yourself rather than take it on faith:

- **CI runs security scanning on every push and PR** (see the badge at the
  top of this README, and [`.github/workflows/security.yml`](.github/workflows/security.yml)):
  [Gitleaks](https://github.com/gitleaks/gitleaks) for committed secrets,
  [Semgrep](https://semgrep.dev/) for static analysis, and
  [Trivy](https://trivy.dev/) for dependency vulnerabilities, container image
  CVEs, and Dockerfile/IaC misconfiguration.
- Those scans have already changed real decisions in this repo — e.g. the
  Docker base image is Alpine instead of Debian-slim specifically because
  Trivy found hundreds of unfixed CVEs in the latter.
- All source is here to read; nothing is obfuscated, minified, or vendored
  without attribution. Issues and PRs are welcome, especially bug reports —
  AI assistance doesn't mean the code is beyond scrutiny, it means you get to
  scrutinize it instead of trusting a vendor's black box.

## Roadmap / ideas

- Re-encode old dailies at a lower bitrate to reclaim space.
- All-sky / long-exposure night camera support (Raspberry Pi HQ + Allsky).
- Object-storage (S3/Garage) backend for videos.

## Contributing

Issues and PRs welcome — especially reports of which Reolink models work. Keep
changes focused and match the existing style.

## License

MIT — see [LICENSE](LICENSE).
