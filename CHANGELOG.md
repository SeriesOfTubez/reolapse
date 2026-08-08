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
- `upgrade.sh` with an explicit **`REOLAPSE_REF` pointing at a branch** upgraded
  to the wrong code, silently. The ref was used verbatim, but the preceding
  fetch only updates remote-tracking refs — so `REOLAPSE_REF=main` reset to the
  machine's *local* `main`, which is stale on a normal install (it resolved to
  the **v0.1.0** commit on a box last upgraded at v0.1.0) and doesn't exist at
  all on the shallow single-branch clone `install.sh` creates, where the upgrade
  failed outright. The ref is now fetched by name first, which works for
  branches, tags, and SHAs alike. Tag upgrades were unaffected.

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
