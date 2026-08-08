# Changelog

All notable changes to ReoLapse are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- Published container images are now **Trivy-scanned before being pushed** to
  GHCR — the publish workflow builds amd64, fails on any fixable HIGH/CRITICAL
  CVE, and only then builds and pushes the multi-arch image. Gates both release
  and `:edge` publishes at publish time (in addition to the existing scan on
  every push/PR to `main`).

### Added
- **Forecast tab: upcoming storms, snow, and moon events.** The counterpart to
  the backward-looking Events tab — the next 10 days of what's worth pointing a
  camera at, so you can plan for a storm instead of finding out afterwards.
  Storms are detected by applying the *same* CAPE/gust/precipitation thresholds
  used for live tagging to forecast hours, so a forecast storm means what a
  detected storm means; retuning the thresholds retunes both. The check runs per
  hour rather than per day, since a day's peak instability and its heaviest rain
  can be twelve hours apart and pairing them would invent storms no real hour
  supports. Rather than invent a confidence score, the tab surfaces what the
  forecasts actually said: probability of precipitation, whether Open-Meteo and
  the NWS agree, and how far out the day is. NWS reaches ~7 days and Open-Meteo
  10, so days 8-10 are marked single-source instead of looking as solid as
  tomorrow. Moon events are computed rather than predicted, so they carry no
  percentage and no uncertainty caveat. New `GET /api/forecast`, cached 30
  minutes; a failed refresh keeps serving the last good forecast labelled with
  its age, because an empty forecast built during an outage is indistinguishable
  from a genuinely calm week. Degrades to moon-events-only with no location
  configured, and works outside the US on Open-Meteo alone. Respects
  `events.weather_enabled` and `events.lunar_enabled`, so a deployment with
  weather tagging off makes no outbound weather calls when the tab is opened and
  one with lunar tagging off never triggers the ephemeris download — the tab
  explains what's switched off rather than just looking empty. Configurable via
  `events.forecast_days` (1-10) and `events.forecast_snow_cm_min`.
- **Per-camera capture schedules.** A camera can now set its own
  `daylight_window` (`enabled`/`mode`/`buffer_minutes`) and `interval_seconds`,
  each falling back to the global `capture` settings independently. This lets a
  single camera record the dark hours — overnight wildlife, a moonrise — while
  the rest keep shooting daylight. A camera can enable its own window while the
  global one is off, or opt out while it is on. Night frames bucket by the
  noon-to-noon day **per camera**, so a night camera in a daytime setup still
  produces one continuous video instead of two halves split at midnight, and its
  build is triggered at its own dawn scoped to just that camera. Editable from
  the Config page under each camera's **Capture schedule**.
- Docker `:edge` image: every push to `main` now publishes a multi-arch
  `ghcr.io/seriesoftubez/reolapse:edge` image (latest development code), the
  Docker parallel to `install.sh`'s `main` option. Run it with
  `REOLAPSE_TAG=edge docker compose pull && docker compose up -d`.

### Fixed
- **Storms were almost never tagged**, so storm bursts and event clips rarely
  fired at all. `storm` relied on Open-Meteo's `weather_code` returning WMO
  95/96/99, which it does in only a minority of real thunderstorms — a storm
  downpour normally reports as *rain showers*. A real deployment ran 19 days
  through repeated storms and logged **zero** storm tags, hence zero event
  videos. `storm` is now corroborated from several signals: NWS alerts (as
  before), **observed** conditions at the nearest NWS station, the WMO code,
  rain combined with high CAPE, and strong wind gusts. CAPE never tags on its
  own — it's convective *potential* and can be high under a clear sky — so it
  only counts alongside actual falling rain. Thresholds are tunable
  (`events.storm_cape_min`, `storm_precip_mm`, `storm_gust_kmh`) and exposed on
  the Config page under **Storm detection tuning**. Replayed against 92 days of
  real hourly weather: storm days detected went from 5 to 17, with 5 dry-hour
  triggers out of 1588 (all genuine gust fronts).
- **A weather API outage no longer cancels an in-progress storm burst.** Every
  source failing produced an empty tag set, indistinguishable from clear skies,
  so a single timeout — and these free services time out regularly — ended a
  burst mid-storm and truncated the event clip. A source that can't be reached
  now keeps its last known tags for `events.stale_grace_minutes` (default: three
  polls) instead of reading as all-clear.

## [0.2.0] - 2026-07-14

### Changed
- `install.sh` now installs the **latest stable release** by default (was
  `main`), and prompts between the release and `main` when run interactively.
  Override with `REOLAPSE_REF=` (a tag or `main`); `REOLAPSE_YES=1` takes the
  stable default non-interactively.
- Docker: `docker compose` now runs a **pre-built multi-arch release image**
  from `ghcr.io/seriesoftubez/reolapse` by default (`docker compose pull`);
  building from source (`up -d --build`) still works. Release images
  (amd64 + arm64) are published automatically on each version tag.

### Added
- **Build status indicator**: the web UI header shows "Building videos…" while a
  daily build is running (the build writes `data/build_status.json`, the UI
  polls it), and refreshes the video list when a build finishes.
- **Night mode** (`capture.daylight_window.mode: night`): capture only the dark
  hours (the inverse of the daylight window). A night spans midnight and is
  saved as one continuous video — frames bucket by a noon-to-noon day — and the
  capture service builds each night automatically ~5 minutes after its window
  closes at dawn (the fixed nightly timer can't, since a night finishes in the
  morning).
- Timezone-accurate capture: set `capture.timezone` (an IANA name like
  `America/Chicago`) or let it auto-detect from `events.zip` /
  latitude-longitude via Open-Meteo (cached to `data/timezone.txt`). Capture
  day boundaries and sunrise/sunset now use that zone instead of the host
  system clock, so a misconfigured host can't split days at the wrong hour.
  The Config page shows the auto-detected zone and lets you override it from a
  dropdown of all IANA time zones.

## [0.1.0] - 2026-07-14

Initial public release.

### Added
- Per-camera **daily** deflickered timelapse, built nightly.
- **Yearly "seasons"** timelapse from a permanent hourly frame archive; holds
  off rendering until enough days exist (`yearly.min_days_before_render`,
  default 30).
- **Weather-aware storm bursts** (NWS + Open-Meteo, no API keys) with dedicated
  per-storm event clips.
- **Lunar** event and **astronomical season** tagging (Skyfield), embedded in
  each frame and in video metadata.
- **PTZ-aware** capture — frames taken away from a camera's home position are
  quarantined out of the videos.
- Works **directly** to a camera or **through an NVR** with a single credential.
- **Web UI**: browse and download daily / yearly / event videos, a Storage
  dashboard with a usage forecast, and a Config page with LAN camera discovery
  and an optional passcode gate.
- **10-second minimum** capture interval to protect the camera/NVR.
- Runs on **Linux + systemd** (`install.sh`) or **Docker Compose**, with an
  in-place `upgrade.sh`.
- Running version reported in the web UI header, the API, service logs, and the
  Docker image.

[Unreleased]: https://github.com/SeriesOfTubez/reolapse/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/SeriesOfTubez/reolapse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SeriesOfTubez/reolapse/releases/tag/v0.1.0
