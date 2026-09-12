# Changelog

All notable changes to ReoLapse are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A failed snapshot no longer leaves a zero-byte JPEG behind.** Frames were
  written straight to their final path, so any partial write — a full disk,
  most obviously — left a 0-byte `.jpg` sitting in the day folder. Those files
  are landmines: ffmpeg's image2 demuxer stops at the first frame it cannot
  decode, so one of them silently truncates that day's video, and if it happens
  to sort first the build fails outright. Snapshots now go to a temp file
  alongside the target and are renamed into place, so a failed write leaves no
  file at all. A single full disk in Sep 2026 left ~1,800 of these across four
  days and cost three days of videos.
- **A corrupt frame no longer passes as a successful build.** `build_video`
  reported the number of frames it *fed to* ffmpeg, not the number that came
  out — so a video truncated at a bad frame was logged as
  `wrote ... (1053 frames, 0.4 MB)` and treated as a clean build. The encode is
  now checked against its input count and fails loudly if it came up short.
  Encoding also happens under a temp name and is only renamed into place once
  verified, so a half-written `.mp4` is never visible to the UI or to pruning.
- **One camera's bad day no longer takes out every camera after it.** An
  unreadable frame raised straight out of `cmd_daily`, so the remaining cameras
  were never attempted and `storage_stats.write_stats()` — which runs at the
  very end — never ran either. That is why the Storage tab could sit frozen on
  days-old figures: the same failure that broke the builds also stopped the
  numbers updating. Each camera is now isolated, and the stats are written even
  when the build fails. The build still exits non-zero if any camera failed.
- **Snapshot pruning no longer accepts a broken video as proof of a build.**
  `prune_old_snapshots` deleted a day's frames once its `.mp4` merely existed,
  never checking it was usable — so a 363 KB, 0.04-second stub from a failed
  encode was enough to authorize deleting the irreplaceable source frames. It
  now refuses to prune behind a video that is unreadable or implausibly short.
- **A failed conditions-log write no longer loses the event permanently.** The
  tag set was marked as "recorded" *before* the line was written, so if the
  write failed — a full disk, again — the next poll saw no change from what it
  believed it had already logged and never retried. A storm could begin and end
  with nothing in the log, leaving no way to rebuild its event clip afterwards
  even though the frames themselves were tagged correctly.
- **A conditions-log failure no longer kills capture.** `conditions.refresh()`
  ran unguarded at the top of the capture loop, so an `ENOSPC` on that write
  propagated out and terminated the process. systemd restarted it 30 seconds
  later, it re-detected the same condition change, and crashed again — a
  restart loop, losing frames on every cycle, for as long as the disk stayed
  full. Weather lookups and log writes are now contained: tags are worth less
  than frames.
- **A truncated conditions-log line no longer breaks a whole date's events.**
  `tag_spans` parsed the log with a bare `json.loads` per line, so one
  half-written line — exactly what an interrupted append leaves — raised and
  took out event building for that date entirely. Unparseable lines are now
  skipped with a warning.
- **Disk usage percentage now matches `df`.** It was computed as
  `used / total`, which ignores that a filesystem's root reserve counts as
  neither used nor free — leaving the gauge reading ~95% on a filesystem an
  unprivileged writer could no longer write to at all.
- **The build indicator no longer vanishes part-way through a build.** A
  `running` status was treated as stale after an hour, measured from when the
  build started — but a full build routinely runs longer than that, so the
  indicator went dark for exactly the builds worth watching. Builds now
  refresh a heartbeat as each camera finishes, and staleness is measured
  against that.
- **A failed build is now visible in the UI.** The header indicator simply hid
  itself when a build failed, so builds could stay broken for days without a
  single sign of it anywhere in the interface. It now reports the last failure
  until a build succeeds.
- **Container image: base OS packages are upgraded at build time.** The
  Dockerfile installed ffmpeg and tini but never upgraded what the base image
  already shipped, so patched versions of pre-installed packages were only
  picked up whenever `python:3.12-alpine` happened to be rebuilt upstream. This
  left 7 HIGH-severity `libuuid`/util-linux CVEs in the published image with
  fixes already available in Alpine.

### Changed
- **Frames are downscaled before deflicker, not after.** The filter chain ran
  `deflicker,scale`, so the deflicker window buffered `deflicker_size` whole
  frames at *source* resolution — ~147 MB for a 5120×1920 camera, versus ~83 MB
  once `max_height` has been applied. On a memory-constrained host that is the
  difference between encoding and swapping. Measured on the reference box, the
  largest camera gained only 1.16× from a second vCPU while the two smaller
  ones gained ~1.73×, because it was blocked on disk (78% iowait) rather than
  CPU. Deflicker smooths exposure variation between frames, which is unaffected
  by doing it after the resize. See the wiki's Storage & Performance page for
  the memory-sizing guidance this produced.

### Added
- **On-demand storage refresh.** The Storage tab now has a **Refresh now**
  button, backed by a new `POST /api/storage/refresh`. The figures were
  previously only rewritten at the end of a successful daily build, so after a
  disk filled, was expanded, or a build failed, the page could quote numbers
  that were days old.
- **Low-storage warning banner.** A banner appears when free space falls below
  10%, below 5%, or is effectively gone, and also when the runway forecast
  drops under a week. Thresholds are on *free* space and runway rather than the
  used percentage alone: this project's own host broke at a reported "98% full"
  — which was 2 GB free against ~1.2 GB/day of growth, about a day of runway,
  and read as perfectly calm right up until writes started failing. The banner
  also says so when the figures behind it are stale.
- **Build progress in the header indicator.** It now reads
  `Building daily videos — 2 of 3 · front-yard · 640/785 (82%)`. The camera
  counter gives the coarse position; within each video, ffmpeg reports its own
  encoded-frame count via `-progress`, which is compared against the known
  input count for a real percentage. Reports are throttled to every 5 seconds,
  and the whole thing degrades quietly — if no progress arrives, the indicator
  falls back to the plain "Building..." message it showed before.
- **Storm and snow sensitivity tiers now say what they trade away.** The slider
  showed only the tier name and its raw numbers (`Conservative — CAPE 1500 J/kg
  · rain 0.5 mm · gusts 80 km/h`), which doesn't tell you what moving it costs.
  Each tier now carries a one-line plain-English description underneath.
- **Dismissable banners.** The update notice and the new storage warning can
  both be dismissed. Dismissals are keyed to the situation — the version, or
  the severity tier — so they come back when something actually changes rather
  than being silenced permanently by one click.

## [0.4.1] - 2026-08-18

### Fixed
- **Tall videos no longer push the player controls off-screen.** The player
  already had a `max-height` meant to keep a video inside the window, but it
  never took effect: it was a percentage of a container that was itself
  content-sized, so the browser resolved it to "no limit". Any timelapse
  taller than the viewport — easy to hit on a high-resolution monitor —
  rendered at full size and shoved the speed/download/delete row below the
  fold. The player now derives a real height from the area around it, so the
  constraint actually applies: an oversized video shrinks to fit the window,
  keeping its aspect ratio, with the controls always visible. Videos that
  already fit are still shown at their native resolution and are never
  upscaled.
- **The Restart services button now actually reloads the page.** It told you
  the page would "disconnect for a few seconds, then reload" — and then never
  reloaded, leaving you on a stale page with no sign the restart had finished.
  The reload was only ever promised, never implemented. It now waits for the
  web service to start answering again and reloads by itself. If the service
  doesn't come back within 45 seconds it says so and points you at
  `systemctl status reolapse-web`, instead of waiting forever.

## [0.4.0] - 2026-08-15

### Added
- **A few small navigation niceties.** The logo/title in the top-left is now
  clickable and takes you back to Daily videos. The version number next to it
  links to that release's notes on GitHub. And when a newer release exists, a
  small banner appears linking straight to its release notes — checked
  against GitHub's releases API, cached for 6 hours so it costs nothing on a
  normal page load, and silent (no banner, no error) if GitHub is unreachable
  or the check fails. New `GET /api/update-check`. A **?** button in the
  top-right opens a small menu linking to the Wiki, GitHub Issues, and GitHub
  Discussions.
- **Storm/snow sensitivity sliders, and a real live-snow threshold.**
  Storm and snow detection thresholds could only be tuned as raw numbers
  buried in the Config page or `config.yaml` — there was no quick "make this
  less trigger-happy" control, and no way to make live snow tagging less
  sensitive at all (the old snow tag was a bare weather-code match with
  nothing to turn down). Both now get a slider on the Config page
  (**Storm detection tuning** / **Snow detection tuning**, under Weather &
  astronomy tagging) with Conservative/Balanced/Aggressive presets;
  Balanced is today's shipped defaults, unchanged. The slider writes
  concrete numbers straight into the existing `events.storm_cape_min` /
  `storm_precip_mm` / `storm_gust_kmh` / (new) `snow_cm_min` keys — no new
  config key is stored for "which preset," so the numbers in `config.yaml`
  are always the whole truth. A raw value that doesn't exactly match a tier
  (hand-edited, or from before this release) renders as "Custom" rather than
  guessing the nearest one. New `events.snow_cm_min` (default `0.0`, cm in
  the reporting interval — not the same unit as the Forecast tab's
  `forecast_snow_cm_min`, which accumulates across a whole day) gates live
  snow tagging the same way the storm thresholds already gate storm tagging,
  and closes the same class of gap the storm-detection corroboration did:
  Open-Meteo reporting measurable snowfall with no 71/73/75 code previously
  produced no snow tag at all. At the default `0.0` this is
  behavior-compatible with the old code-only rule. Both sliders govern the
  Open-Meteo signal only — NWS alerts and station observations are keyword
  matches and always tag regardless (a Winter Storm Warning tags snow no
  matter what's set), which is a pre-existing, now-documented limitation
  rather than a new one. Global only — every camera still shares one
  weather location, so a per-camera threshold would change how
  trigger-happy a camera is about the same measurement, not what it
  actually sees; `events_enabled: false` remains the per-camera opt-out.
- **Config page: App Settings / Cameras tabs.** The Config page was one long
  scroll through nine sections regardless of what you came to change. It's
  now split into two tabs: **Cameras** (the camera list/picker, each
  camera's full settings card, and network discovery) and **App Settings**
  (everything global — Capture, Storage, Daily video, Yearly video, Weather &
  astronomy tagging, Event video clips, Web UI, and Config page access).
  Switching tabs never touches the network or discards an unsaved edit — both
  tabs read and write the same in-memory config, and **Save config** in the
  shared footer still saves everything regardless of which tab is showing.
  The Cameras tab reuses the camera picker introduced for the navbar above,
  selecting by list position rather than name (camera names are editable and
  can transiently collide, e.g. right after "+ Add camera manually", which
  starts every new camera out named the same thing).
- **Navbar rework: a camera picker plus a Daily/Yearly/Events dropdown.**
  Replaces the old row of one pill button per camera (which stopped scaling
  past a handful of cameras) with a single `<select>`, shared by the navbar
  and, further down this list, the Config page's Cameras tab. The three
  video-type pills (Daily/Yearly/Events) become a second dropdown next to it;
  Forecast/Storage/Config stay as pills, since only one of those three is ever
  "active" the way a tab is. On a view that doesn't use a camera (Forecast,
  Storage, Config) the camera picker stays visible but disabled, with a
  tooltip explaining why, rather than hiding and reflowing the header or
  silently doing nothing when changed. Fixes a bug where the camera selection
  reset to the first camera every time a nightly build finished refreshing the
  page in the background — browsing camera 3 no longer bounces you back to
  camera 1 mid-session.
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

[Unreleased]: https://github.com/SeriesOfTubez/reolapse/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/SeriesOfTubez/reolapse/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/SeriesOfTubez/reolapse/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/SeriesOfTubez/reolapse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/SeriesOfTubez/reolapse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SeriesOfTubez/reolapse/releases/tag/v0.1.0
