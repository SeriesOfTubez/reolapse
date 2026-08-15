# Storage & Performance

## Storage estimates

<p align="center">
  <img src="https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/assets/screenshot-storage.png" width="820" alt="ReoLapse Storage tab: stat cards for disk used, snapshots, daily and yearly video sizes and average build time; a storage runway forecast banner; and a per-camera usage table">
</p>

The **Storage** tab in the web UI shows live per-camera and system-wide usage
and daily growth (`storage_stats.py`, refreshed after every nightly build —
no guessing required once it's running). Before you get there, here's real
data from the reference deployment (3 Reolink cameras of different
resolutions, 1 frame/minute, all-day capture) to help you size disk up front:

| Camera | Resolution | Avg JPEG size | Raw snapshots/day | Daily video/day |
|---|---|---|---|---|
| Reolink Wired WiFi Doorbell | 2560×1920 (4.9 MP) | ~450 KB | ~0.6 GB | ~200 MB |
| Reolink TrackMix WiFi (Wide Cam) | 3840×2160 (8.3 MP) | ~1.3 MB | ~1.8 GB | ~290 MB |
| Reolink OMVI 3i WiFi (Fixed Cam) | 5120×1920 (9.8 MP) | ~1.7 MB | ~2.4 GB | ~470 MB |

Rough rule of thumb: **~0.1–0.2 MB per megapixel per frame**, varying with
scene complexity and each camera's own JPEG quality setting — the range above
spans 3 real cameras and isn't a tight line, so treat it as a ballpark, not a
formula. For a precise number, check the actual size of a few files in
`data/snapshots/<camera>/` and multiply by how many frames/day you'll capture
(`86400 / capture.interval_seconds`, or less if `start_time`/`end_time` is set).

What accumulates and what doesn't, and how to bound each:

- **Raw snapshots** are a *rolling window* — pruned `storage.keep_snapshots_days`
  after each day's video builds, so this cost is already bounded by default.
- **Daily videos, yearly videos, and event clips default to forever** (`0` in
  `daily_video.retention_days`, `yearly.retention_years`,
  `events_video.retention_days`) but each is independently configurable. In
  the reference 3-camera deployment, daily videos alone grow by **~0.9
  GB/day** — roughly **28 GB/month** or **340 GB/year** — unbounded by
  default, before adding storm/snow clips or a fourth camera.
- **Yearly *archive frames* (`data/yearly_frames/`) are never prunable, by
  design** — that's the permanent, irreplaceable source the yearly video is
  built from. This is why pruning a *yearly video* is cheap and safe: its
  frames are untouched, so `build_timelapse.py yearly --year YYYY`
  regenerates it any time. The frame archive itself is small regardless
  (well under 100 MB/day across 3 cameras) — it's not what threatens your disk.

### Retention & the storage forecast

The Storage tab shows your **current retention settings** for all four tiers
side by side, plus a **forecast**: it computes the eventual steady-state size
of every tier that has a retention limit set, and a growth rate for whatever
doesn't (the yearly archive frames always contribute here, since they can't
be bounded). From that it reports one of three verdicts against your current
free disk space:

- **Runway** — some tier still grows forever; shows how long until disk
  fills at the current rate (e.g. "~20 days").
- **Headroom** — every tier is bounded and there's room to spare once each
  reaches its ceiling.
- **Shortage** — the bounded tiers' ceilings alone already exceed your free
  space, before any unbounded growth is even considered.

This recalculates after every nightly build, so it tracks reality as your
retention settings, camera count, or event frequency change — no manual math
required.

## Performance

Reference deployment: a Proxmox VM with **1 vCPU and 1 GB RAM** (Ubuntu 26.04
cloud image), on a Proxmox host with an **Intel N95**. With 3 cameras and a
full day of frames (~1440/camera), the nightly build takes about **45–50
minutes** total and peaks around **700 MB RAM**. Encoding (`libx264`, default
preset `medium`) is the bottleneck; the hardlink/copy staging step is
comparatively instant.

Cameras currently build **sequentially, one at a time** (not in parallel), so
total build time is roughly the sum of each camera's encode time. Within a
single camera's encode, though, `libx264` threads automatically across
whatever cores are available — so **more vCPUs should speed up each camera's
build** (diminishing returns past ~8 threads), and a faster single-thread CPU
shortens it further. Neither has been benchmarked beyond the reference
1-vCPU deployment above; reports from other hardware are welcome.

If build time is a problem before you can add cores: lower `daily_video.max_height`
(fewer pixels to encode), `capture.interval_seconds` (fewer frames/day), or
the `preset` for the video type you're building (`daily_video.preset`,
`yearly.preset`, `events_video.preset` — faster x264 presets trade a larger
file for less CPU time). All are editable from the Config page.

The Storage tab tracks your **own** build times (an "avg build time" card,
averaged over the last 60 nightly builds) — that's the number to trust for
your actual hardware and camera count, not the reference figures above.
