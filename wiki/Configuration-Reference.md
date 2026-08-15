# Configuration Reference

## Configuration

Everything lives in `config.yaml` (copy from `config.example.yaml`). Secrets do
not: reference them as `${VAR}` and put the values in `.env`. Highlights:

| Key | Meaning |
|---|---|
| `cameras[].host` / `channel` | Camera IP + `0`, or NVR IP + channel number |
| `cameras[].ptz_home` | Quarantine frames taken off a PTZ camera's home position |
| `cameras[].daylight_window` | Per-camera day/night schedule; each key falls back to `capture.daylight_window` — see [Per-camera schedules](PTZ-and-Night-Capture#per-camera-schedules) |
| `cameras[].interval_seconds` | Per-camera capture cadence; falls back to `capture.interval_seconds` |
| `cameras[].events_enabled` | `false` makes a camera ignore events entirely — no burst capture, no event clips (default `true`) — see [Cameras that sit out events](PTZ-and-Night-Capture#cameras-that-sit-out-events) |
| `cameras[].include_events_in_daily` | Per-camera override of `daily_video.include_events`; omit the key to follow the global setting |
| `capture.timezone` | IANA timezone for capture timing/day boundaries; blank auto-detects from location, else falls back to the host clock |
| `capture.interval_seconds` | Base capture cadence (default 60, minimum 10) |
| `capture.start_time`/`end_time` | Optional fixed daily capture window |
| `capture.daylight_window` | Capture only around actual sunrise/sunset instead of a fixed clock window — see [Night capture & IR cameras](PTZ-and-Night-Capture#night-capture--ir-cameras) |
| `storage.keep_snapshots_days` | Retention for raw frames after their video builds |
| `daily_video.deflicker_size` | Deflicker window; `0` disables |
| `daily_video.retention_days` | Delete a daily video this many days after its date; `0` = forever |
| `daily_video.include_events` | Weave storm/snow **burst** frames into the daily video (default `false`) — see [Event frames in the daily video](Weather-and-Storm-Detection#event-frames-in-the-daily-video) |
| `yearly.min_days_before_render` | Wait until this many days are archived before rendering the yearly video (default 30); `0` renders as soon as any frames exist, `yearly --force` overrides once |
| `yearly.video_frames_per_day` / `video_window` | Pacing of the yearly video |
| `yearly.retention_years` | Delete a yearly video once it's this many years old; `0` = forever. Cheap to set low — see [Storage estimates](Storage-and-Performance#storage-estimates) |
| `events.weather_enabled` | Storm/snow/rain tagging + burst capture (needs `events.zip` or `latitude`/`longitude`) |
| `events.lunar_enabled` | Moon-event tagging — no location required |
| `events.season_enabled` | Spring/summer/fall/winter tagging on frames + video metadata — no location required |
| `events_video.tags` | Which tags get their own `<date>_<tag>.mp4` clip (default `storm`, `snow`; any tag works, including moon events) |
| `events_video.deflicker_size` / `deflicker_by_tag` | Deflicker for event clips — off by default (protects lightning in storm clips), overridable per tag (e.g. enable for `snow`) |
| `events_video.retention_days` | Delete an event clip this many days after its date; `0` = forever |
| `webapp.accent_color` | UI accent color: `amber` (default), `green`, `blue`, `red`, `purple`, `yellow` |

See the inline comments in `config.example.yaml` for the full reference.

## Config page & network discovery

The **Config** tab edits `config.yaml` from the browser instead of by hand —
every setting above except secrets (see below) is exposed as a checkbox,
dropdown, radio, or text field.

<p align="center">
  <img src="https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/assets/screenshot-config.png" width="820" alt="ReoLapse Config page: editing a camera in the browser — name, host, channel, username, a password field that's a dropdown of .env variable references, and HTTPS / verify-SSL toggles, with Save config and Restart services buttons">
</p>

- **Passwords are always a variable reference, never a real value, in the
  UI.** Each camera's password field is a dropdown of `REOLINK_PASSWORD*`
  variables this server has loaded from `.env` (names only — the actual
  values never reach the browser); pick one, or choose "Custom" to reference
  a variable you haven't added to `.env` yet. The save endpoint independently
  rejects anything that isn't a `${VAR}` reference, so a literal password
  typed into the form can't end up in `config.yaml` even if the UI is
  bypassed.
- **Fields the UI doesn't have a control for are preserved as-is.** The page
  edits the config it fetched in place rather than rebuilding it from
  scratch, so things like a camera's `ptz_home` block or
  `events_video.deflicker_by_tag` survive a save untouched even though
  there's no form control for them yet.
- **Saving does not restart anything by itself.** The web UI picks up most
  changes on next page load (accent color is immediate), but `capture.py` and
  `build_timelapse.py` are separate processes — they only pick up a saved
  change after a restart.
- **Restart services button.** On a systemd deployment, the Config page's
  footer has a **Restart services** button that runs
  `systemctl restart reolapse-capture.service` and
  `reolapse-web.service` for you, so you don't need shell access just to
  apply a config change. It requires a narrowly-scoped passwordless sudo rule
  (only those two restart commands, nothing else). The **easy installer sets
  this up automatically**; for a manual install, install the ready-made
  `deploy/reolapse.sudoers` drop-in (see
  [Installation](Installation#install-on-a-linux-vm-systemd)). Without the rule
  the button just fails with a clear error instead of hanging. Docker
  deployments don't have `systemctl` at all — the button detects this and
  tells you to run `docker compose restart` instead.
- **Comments are not preserved.** This editor round-trips the YAML as data,
  not text, so saving from the UI strips out `config.yaml`'s hand-written
  comments. A backup of the previous file is written to `config.yaml.bak`
  before every save.
- **Config page access (optional passcode).** The **Config page access**
  section at the bottom of the page lets you set a single passcode that gates
  this page and its write/scan endpoints — video browsing stays open. Setting
  the first passcode is allowed from the open page (it logs you straight in);
  changing or removing it afterward requires being logged in. See
  [Security](Security) for how it's stored and what it does and doesn't
  protect. Setting or changing the passcode also rewrites `config.yaml` (same
  `.bak` backup, same comment-stripping caveat as above).
- **Network discovery** (inside the Cameras section) scans your `/24` for
  Reolink devices and lists the ones it finds. An unauthenticated probe can
  only confirm a device is there, not what it is — click **Identify & add**
  on a result, supply credentials, and it fetches the real model/name/channel
  list before adding anything. The credentials you type there are used for
  that one lookup only and are never saved; you still need to add the real
  password to `.env` yourself before restarting capture. If a scan seems to
  miss a device, try it again — the first scan after a service restart can
  occasionally undercount on constrained hardware (observed on a 1-vCPU
  reference VM; consistently found everything on immediate re-runs).
  Discovery only sees whatever network the server itself is on — inside
  Docker's default bridge network, that's the container's private subnet,
  not your LAN; run outside Docker or add `network_mode: host` if you want
  discovery to work in a container.
