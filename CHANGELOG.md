# Changelog

All notable changes to ReoLapse are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/SeriesOfTubez/reolapse/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SeriesOfTubez/reolapse/releases/tag/v0.1.0
