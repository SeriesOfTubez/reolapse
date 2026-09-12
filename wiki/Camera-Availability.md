# Camera Availability & On-Demand Builds

The **Cameras** tab answers two questions that used to need an SSH session:
*did every camera actually capture what it was supposed to?*, and *can I
rebuild that day?*

## Availability

For each camera, for each of the last 14 days, it reports:

- **Coverage** — frames captured against frames *scheduled*, as a bar and a
  percentage.
- **Gaps** — when capture stopped, as clock times, plus the longest single
  outage.
- **A midnight-to-midnight strip** — 24 cells showing which hours lost frames,
  so a camera that drops out every night at the same time is obvious at a
  glance.
- **Reasons** — why frames were missed, when the cause was recorded.

### How the numbers are worked out

Availability is **reconstructed from the frames on disk**, not from a counter
kept by a running process. That is deliberate: it means history from before
this feature existed still reports, and a capture process that crashed or was
restarted cannot quietly zero its own numbers.

Two details do most of the work:

**The denominator is each camera's own schedule.** Expected frames are the
ticks that camera was scheduled to take — its own `interval_seconds`, gated on
its own capture window — not the number of minutes in a day. Measured against
wall-clock instead, a camera limited to daylight (or a night camera) would read
as permanently ~50% down.

**Gaps are bounded by that schedule, not just by the frames.** A gap found only
*between* two frames misses the most common real failure: a camera that stops
and never comes back has no later frame to measure against, so it would report
no gap at all — which is exactly the shape a full disk produces. Bounding the
search by the first and last scheduled tick catches an outage that runs to the
end of the day, and a day that starts late.

### Reading over 100%

The denominator uses the camera's **base** interval. While a burst tag is
active (see [Weather & Storm Detection](Weather-and-Storm-Detection)) capture
speeds up, which only ever *adds* frames, so a stormy day can land slightly
over 100%. It is clamped for display.

Sizing the denominator by burst instead would mean reconstructing every burst
span from the conditions log — and would get *less* accurate exactly when that
log has holes in it, which is precisely when you are looking at this page.

### Days whose frames are already gone

Snapshots are pruned `storage.keep_snapshots_days` after their video is built.
Past that window there are no frames left to count, so availability falls back
to counting the frames in the **built video**, and says so.

Treat those days as a floor, not a fact: the daily video leaves out event-burst
frames when `daily_video.include_events` is off, so a stormy day will read
lower than it actually was. Days with neither frames nor a video report nothing
rather than 0%.

### Night-mode cameras

A night camera's folder spans noon-to-noon so one night lands in one file, which
means its scheduled window crosses midnight. Leading and trailing gap detection
is skipped for those cameras rather than reporting times that would be wrong —
coverage and between-frame gaps still report normally.

### Why frames were missed

A failed snapshot is recorded to `data/capture_misses.jsonl` with a reason:

| Reason | Means |
|---|---|
| `unreachable` | the camera refused the connection or could not be resolved — power, network, or a wrong host |
| `timeout` | the camera accepted the connection but did not answer in time |
| `bad_response` | HTTP 200 that was not a JPEG — usually bad credentials, which Reolink returns as a JSON error body |
| `http_error` | the camera returned an HTTP error status |
| `write_failed` | the frame arrived but could not be written to disk — nearly always a full disk |
| `error` | anything else |

The distinction between `unreachable` and `write_failed` is the point. Both
produce a missing frame, and treating them alike turns "the disk is full" into
"all three cameras went offline at once" — which sends you looking in entirely
the wrong place. Camera passwords are stripped from the recorded detail, the
same way they are from the log.

This log only covers failures since the feature existed. Coverage percentages
and gaps do not depend on it, so older days still report — just without a
reason attached.

## Building or rebuilding a day

**Build videos for a date** at the top of the tab takes a date and, optionally,
a single camera. Daily videos in the player also get a **rebuild** button, which
re-encodes that day for that camera.

Useful when:

- a nightly build failed and the day never got a video;
- a day's video is truncated or corrupt and you want it re-encoded from the
  frames;
- you changed `daily_video` settings (`max_height`, `crf`, `preset`, `fps`) and
  want to see them applied to a day you already have.

Things worth knowing:

- **Only works while the source frames survive.** Past
  `storage.keep_snapshots_days`, a day's frames are gone and it cannot be
  rebuilt — which is also why pruning now refuses to delete frames sitting
  behind an unreadable or implausibly short video.
- **One build at a time.** A second request while one is running is refused.
  Two concurrent encodes on a small host is how a slow build becomes a swapping
  one — see [Storage & Performance](Storage-and-Performance) — and they would
  fight over the same output paths.
- **It runs detached.** Builds routinely outlive the request that started them
  by an hour or more; progress shows in the header indicator, with the camera
  position and the encoded-frame percentage.
- **The existing video is only replaced once the new encode is verified.** A
  failed or truncated rebuild leaves the old file untouched.
- **Gated by the Config-page passcode** when one is set — see
  [Security](Security).
- Yearly and event videos have no rebuild button, because they are built by
  different subcommands (`build_timelapse.py yearly` / `events`). Yearly videos
  are always rebuildable from the frame archive, which is never pruned.
