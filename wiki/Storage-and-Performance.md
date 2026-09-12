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
  Pruning checks that the video is actually *usable* first, not merely that the
  file exists: a day's frames are irreplaceable, and a truncated or unreadable
  video is no proof the day was built successfully. A day sitting behind a bad
  video keeps its frames and logs a warning instead — rebuild it (see
  [Camera Availability](Camera-Availability)) and the space is reclaimed on the
  next pass.
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

### Low-space warnings, and keeping the figures honest

A banner appears when free space drops below **10%**, below **5%**, or is
effectively gone, and also when the **runway forecast falls under a week**. It
can be dismissed, and returns if the situation gets worse.

The thresholds are on free space and runway rather than the used percentage
alone, because that percentage on its own is a poor alarm. The reference
deployment broke at a reported **98% full** — which was 2.0 GB free against
~1.2 GB/day of growth, about 1.4 days of runway, and read as perfectly calm
right up until writes started failing.

One thing to know about where these numbers come from: `storage_stats.json` is
written at the **end of a successful daily build**. A stretch of failed builds
therefore leaves the Storage tab quoting figures that are days old — exactly
when accurate free space matters most. The banner says so when its own figures
are stale, and the Storage tab has a **Refresh now** button that recomputes
them on demand. Worth pressing after a disk fills up, gets expanded, or a build
fails.

Note also that the percentage is computed the way `df` reports `Use%` —
`used / (used + free)`. Where a filesystem sets a root reserve (ext4 defaults to
5%, though cloud images often ship with none) those blocks belong to neither
figure, so `used / total` would read ~95% on a filesystem an unprivileged
writer could no longer write to at all.

### Deleting a video by hand

A **delete** button sits next to **download** in the player, for clearing out
a junk video (camera glare, a false storm trigger) the moment you spot it
instead of waiting on retention settings to catch it. What deleting each
video type actually costs differs, and the confirmation dialog states the
real rule rather than a generic warning:

- **Yearly videos are always rebuildable** — `build_timelapse.py yearly
  --year YYYY --force` regenerates one any time, since the frame archive it's
  built from is never pruned.
- **Daily and event videos are only rebuildable while their source frames
  still exist** — `build_timelapse.py daily --date YYYY-MM-DD --camera X` (or
  `events` for an event clip), which only works within
  `storage.keep_snapshots_days` of that day. Past that window, deletion is
  permanent.

Deleting an event clip also drops its `events.jsonl` index entry immediately,
rather than waiting for the next nightly build to notice the file is gone.
Deleting any video does **not** free the snapshot frames it was built from —
those are governed by their own retention (`storage.keep_snapshots_days`),
independent of what videos exist.

This is a write/delete action, so it's gated by the Config-page passcode when
one is set — see [Security](Security).

## Performance

### Sizing: memory first, then CPU

**Give the machine at least 2 GB of RAM — more than it needs for CPU.** Build
time is dominated by `libx264`, but on an undersized box the encode does not
fit in memory and the build becomes disk-bound instead, at which point extra
vCPUs buy you very little.

The reference deployment is a Proxmox VM (Ubuntu 26.04 cloud image) on an Intel
N95 host, with 3 cameras at 1 frame/minute. Measured per-frame encode times,
1 vCPU vs 2 vCPU, both at **1 GB RAM**:

| Camera | Source | Scaled to 1440 | 1 vCPU | 2 vCPU | Speed-up |
|---|---|---|---|---|---|
| Doorbell | 2560×1920 | 1920×1440 | 0.485 s/frame | 0.283 s/frame | **1.72×** |
| TrackMix | 3840×2160 | 2560×1440 | 0.792 s/frame | 0.456 s/frame | **1.74×** |
| OMVI 3i | 5120×1920 | 3840×1440 | 2.027 s/frame | 1.741 s/frame | **1.16×** |

The two smaller cameras scale almost ideally with the second core. The largest
one barely moves — at 1 GB RAM its encode holds ~564 MB resident, the host
starts swapping, and `top` shows **78% iowait against 13% user time**. It is
waiting on disk, not computing. That one camera is ~64% of total build time, so
the bottleneck for the whole build is memory, not cores.

Rules of thumb:

- **Under ~4 MP per camera:** 1 GB works; add vCPUs for close to linear gains.
- **Above ~4 MP, or more than two cameras:** **2 GB minimum**, 4 GB comfortable.
  Below that, adding vCPUs is largely wasted.
- Swap is not a substitute. It is on the same disk the build reads frames from
  and writes video to, so swapping competes with the build's own I/O.

### Where the memory goes

Two parts of the encode dominate, and both scale with resolution:

- **The deflicker window** buffers `daily_video.deflicker_size` whole frames.
  These are buffered *after* the downscale, so `max_height` bounds them: at
  `deflicker_size: 10` and 3840×1440 that is ~83 MB.
- **x264's rate-control lookahead** holds `rc-lookahead` frames at output
  resolution — **40 frames** on the default `medium` preset, ~330 MB at
  3840×1440. This is usually the larger of the two.

So a single 5120×1920 camera at `max_height: 1440` and `preset: medium` needs
roughly 450–550 MB for the encode alone, before Python, the OS, and page cache.

### If you cannot add memory

In rough order of effect:

- **A faster `preset`** (`veryfast`, `faster`). This cuts `rc-lookahead`
  sharply — by far the biggest single memory saving — at the cost of a larger
  file for the same `crf`.
- **Lower `daily_video.max_height`** (1080 instead of 1440 cuts frame bytes
  ~44%, and shrinks both buffers above).
- **Lower `deflicker_size`**, or set it to `0` to disable deflicker entirely.
- **Raise `capture.interval_seconds`** — fewer frames per day to encode.

All are editable from the Config page.

### Build time in practice

Cameras build **sequentially, one at a time**, so total build time is roughly
the sum of each camera's encode. On the 1 vCPU / 1 GB reference box with 3
cameras and a full day each (~1440 frames), nightly builds ran **49–85
minutes**, varying with frame count and scene complexity. Within a single
camera, `libx264` threads across whatever cores are available (diminishing
returns past ~8 threads) — but only once the encode actually fits in RAM.

The Storage tab tracks your **own** build times (an "avg build time" card,
averaged over the last 60 nightly builds) — that's the number to trust for
your hardware and camera count, not the reference figures above. Reports from
other hardware are welcome.
