# Changelog

All notable changes to ReoLapse are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Optional event-burst frames in the daily video.** While a storm/snow burst
  is active, capture drops to `events.burst_interval_seconds`, so those
  minutes land in the same day folder far denser than the rest of the day and
  the daily video crawls through them. New `daily_video.include_events`
  (default `false`) leaves frames inside an active burst span out of the
  daily `.mp4` only — the event clip, the yearly frame archive, and the
  frames on disk are all untouched, and a day that was event-tagged end to
  end skips the daily video rather than rendering an empty one (the yearly
  archive still gets that day's frames). Any camera can override the global
  setting with `include_events_in_daily`. Skipped entirely for
  `events_enabled: false` cameras, since they never burst and have nothing
  extra to exclude. Editable from the Config page under **Daily video** and
  each camera's **Weather events**. Defaults to off; existing configs and
  existing daily videos are unaffected.
- **Delete a video straight from the player.** A **delete** button next to
  **download** removes the selected daily, yearly, or event video from disk —
  useful for clearing out a junk clip (camera glare, a false storm trigger)
  the moment you spot it, instead of waiting for retention settings to catch
  it. New `DELETE /api/videos/<camera>/<vtype>/<name>`, gated by the same
  optional Config-page passcode as every other write endpoint (`/api/config`,
  `/api/restart`) — with no passcode set, deletion is open to the LAN, same as
  those. Deleting an event clip also drops its `events.jsonl` entry
  immediately rather than waiting for the next nightly build to notice it's
  gone. `/videos/<camera>/<vtype>/<name>` (the unauthenticated browse route)
  now validates the camera name too, closing a gap where only `name` and
  `vtype` were checked. Existing configs are unaffected.
- **Configurable merge gap for event clips.** Two spans of the same tag close
  together used to always merge into one clip after a fixed 20-minute gap.
  That gap is now `events_video.gap_minutes` (still defaults to 20), with an
  optional `gap_minutes_by_tag` override for individual tags — a slower-moving
  snow event can tolerate a longer lull than a storm before it's split into a
  separate clip. The event-clip builder and the daily video's event-frame
  exclusion filter (`daily_video.include_events`) both reconstruct spans
  through the same resolver, so they always agree on where one event ends and
  the next begins, regardless of which one changes. Editable from the Config
  page under **Event video clips**; per-tag overrides are config-file only.

### Changed
- **`events_video.min_frames` is renamed to `events_video.min_seconds`, and
  now pads short clips instead of dropping them.** The minimum-clip-length
  setting is a duration; a key literally named `min_frames` that actually
  meant "roughly one second per unit" was misleading. `min_seconds` (default
  `5`) is converted to a frame count via `events_video.fps` at build time.
  Every tagged span gets a clip now, even a brief one — a span shorter than
  the minimum is padded out with extra frames from before/after the event
  instead of being skipped, so a real but short-lived storm alert still gets
  a watchable clip rather than silently producing nothing. Existing configs
  keep working unchanged —
  `min_frames` is still honored if `min_seconds` is absent, so a config.yaml
  that already sets `min_frames: 30` keeps its old ~1-second floor forever
  unless you act. To adopt the new 5-second default, replace `min_frames: 30`
  with `min_seconds: 5` in config.yaml, or just open the Config page, which
  migrates it for you automatically the moment you view **Event video clips**.

### Fixed
- **An event still active at midnight produced zero frames and no clip.**
  `tag_spans()` closed a still-open span at end-of-day using a bound that
  formatted as `"000000"`, which a string comparison against frame timestamps
  can never match. Late-running storms/snow events now close at `23:59:59`
  instead, so they get the clip (and, post event-frame-in-daily-video, the
  correct daily-video exclusion) they should have all along.

## [0.3.0] - 2026-08-08

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
- **Per-camera event opt-out.** A camera can now set `events_enabled: false` to
  ignore weather/lunar events entirely — it holds its own `interval_seconds`
  through a storm instead of dropping to `events.burst_interval_seconds`, and no
  event clips are built from its frames. For a camera the weather isn't visible
  from (indoors, a doorway, a tight framing) the burst frames are just disk and
  the clip is a video of nothing happening. The burst interval is now resolved
  **per camera**, so the rest of the setup still bursts normally. Frames are
  still tagged either way, so the metadata stays complete for the search/filter
  features built on it. Defaults to true; existing configs are unaffected.
  Editable from the Config page under each camera's **Weather events**.
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

[Unreleased]: https://github.com/SeriesOfTubez/reolapse/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/SeriesOfTubez/reolapse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/SeriesOfTubez/reolapse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SeriesOfTubez/reolapse/releases/tag/v0.1.0
