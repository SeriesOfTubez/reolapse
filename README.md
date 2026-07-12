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

---

## Features

- **Daily timelapses** per camera, with ffmpeg `deflicker` to tame
  auto-exposure flicker.
- **Yearly "seasons" timelapse.** Frames are archived hourly and kept forever;
  the yearly video is rendered from a configurable, re-tunable subset
  (e.g. 10 frames/day within daylight hours ≈ a 2-minute year).
- **Weather-aware storm bursts.** Polls NWS alerts + Open-Meteo (free, no API
  keys) and computes moon events locally. During storms/snow it captures every
  10s instead of every 60s, and the nightly build cuts a per-storm clip.
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
essentially all current cameras and NVRs. You can connect two ways:

- **Directly** to a camera (`host` = camera IP, `channel: 0`) — full-resolution
  main-stream snapshots, no NVR dependency. Preferred when reachable.
- **Through an NVR** (`host` = NVR IP, `channel` = the camera's channel) — one
  credential covers every channel; use this when cameras sit on the NVR's
  isolated PoE network.

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
                     ├─►  data/videos/<cam>/events/<date>_storm.mp4
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
| `weather.*` | Storm/snow burst capture + condition tagging (US alerts) |
| `events_video.tags` | Which tags get their own event clips |

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

With `weather.enabled: true` and your location set (`zip`, or `latitude`/`longitude`), capture polls
NWS + Open-Meteo every `poll_minutes`. Active tags are appended to
`data/conditions/<date>.jsonl` and embedded in each frame as a JPEG comment
(`{"tags":["storm"]}`, visible in exiftool). Storms/snow trigger burst capture,
and the nightly build renders a clip per event span into the **Events** tab
(deflicker off, so lightning flashes aren't smoothed away). Moon events —
including blood moons (total lunar eclipses) — are computed locally with
[Skyfield](https://rhodesmill.org/skyfield/); the JPL ephemeris it needs
(`de421.bsp`, ~17 MB) downloads once on first run into `data/ephemeris/`.

## PTZ cameras

Add a `ptz_home` block (see `config.example.yaml`). Before each snapshot,
capture reads `GetPtzCurPos` and compares pan/tilt to the configured home;
off-home frames go to an `offposition/` subfolder — excluded from videos, still
pruned normally. Whichever axes the response includes are checked; an NVR
relays only pan (usually enough). Set `ptz_home.host` to the camera's own IP if
you need the tilt axis. The check fails open — a position-query error keeps the
frame.

## Security

- Credentials live in `.env` (gitignored), never in `config.yaml`.
- Prefer a **dedicated, least-privilege** camera/NVR account for ReoLapse. The
  Snap API passes credentials as URL parameters, so avoid `&`, `#`, `%` in that
  password.
- The bundled Flask server is for trusted LAN use. Put it behind a reverse
  proxy with auth/TLS before exposing it.

## Roadmap / ideas

- Re-encode old dailies at a lower bitrate to reclaim space.
- All-sky / long-exposure night camera support (Raspberry Pi HQ + Allsky).
- Object-storage (S3/Garage) backend for videos.

## Contributing

Issues and PRs welcome — especially reports of which Reolink models work. Keep
changes focused and match the existing style.

## License

MIT — see [LICENSE](LICENSE).
