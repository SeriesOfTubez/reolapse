# Changelog

All notable changes to ReoLapse are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
