# PTZ & Night Capture

## PTZ cameras

Add a `ptz_home` block (see `config.example.yaml`). Before each snapshot,
capture reads `GetPtzCurPos` and compares pan/tilt to the configured home;
off-home frames go to an `offposition/` subfolder — excluded from videos, still
pruned normally. Whichever axes the response includes are checked; an NVR
relays only pan (usually enough). Set `ptz_home.host` to the camera's own IP if
you need the tilt axis. The check fails open — a position-query error keeps the
frame.

## Night capture & IR cameras

If a camera relies on IR illumination at night, a 24-hour daily timelapse
will show a jarring color-to-black-and-white-and-back transition at the start
and end of every night — deflicker smooths *exposure* flicker, not a full
color-mode switch. Two ways to avoid it:

- **Disable IR** if the scene has enough ambient light without it (street
  lights, a porch light, etc.) — the camera stays in color all night, at the
  cost of more visible sensor noise in the dark. This is the reference
  deployment's setup and it works well; see the
  [Weather tagging](Weather-and-Storm-Detection#weather-tagging--storm-bursts)
  and [Lunar event detection](Lunar-and-Season-Tagging#lunar-event-detection)
  wiki pages for why full 24-hour color capture is worth having if you can.
- **Capture only during daylight** with `capture.daylight_window` if IR needs
  to stay on. Unlike a fixed `start_time`/`end_time`, this is computed fresh
  every day from real sunrise/sunset for your location (`events.zip` or
  `latitude`/`longitude`, independent of whether weather/lunar tagging is
  enabled), so it tracks the seasons instead of drifting out of sync with
  them. `buffer_minutes` extends the window a bit past sunrise/sunset on each
  end, since IR typically doesn't kick in the instant the sun sets — tune it
  down if you still catch IR frames, or up if you're cutting off usable
  daylight.

We looked at a third option — asking the camera directly whether it's
currently in color or IR mode via `GetIsp`'s `dayNight` field — but that
field is the camera's **configured mode** (`Auto`/`Color`/`Black&White`), not
a live readout of which one is currently active. For a camera left on
`Auto` (the normal case), querying it just returns `"Auto"` and tells you
nothing about the moment-to-moment state, so it can't drive a per-frame
decision. The sunrise/sunset approach above doesn't have that problem.

### Night-only timelapses (night mode)

Set `capture.daylight_window.mode: night` (with the window `enabled`) to do the
**opposite** of daylight capture — record only the **dark hours** and skip the
day. It reuses the same sunrise/sunset math, just inverted; `buffer_minutes`
trims that much twilight off each end of the night instead of extending it.

Because a night spans midnight, night mode buckets frames by a **noon-to-noon
day**, so one evening plus the following morning become **one continuous video**
(labeled by the night's start date) instead of two half-clips split at midnight.
And since a night isn't finished until dawn, the nightly timer can't build it —
the **capture service builds each night automatically ~5 minutes after its
window closes at sunrise**. (A manual build still works:
`build_timelapse.py daily --date YYYY-MM-DD`.)

Night mode needs a location set (`events.zip` or `latitude`/`longitude`), same
as the daylight window. Leave `yearly.video_window` empty when using it — a
daylight-hours filter makes no sense for night frames.

### Per-camera schedules

Everything above can be set **per camera**, so one camera can watch the dark
hours while the rest shoot daylight — a yard camera logging which animals visit
overnight, or one framing a moonrise, without changing what the others do:

```yaml
cameras:
  - name: wildlife
    # ...host, credentials, etc...
    interval_seconds: 300     # optional; falls back to capture.interval_seconds
    daylight_window:
      enabled: true
      mode: night
      buffer_minutes: 30
```

Each key falls back to the global `capture.daylight_window` **independently**,
so `mode: night` alone inherits the global buffer. The two are resolved
separately, so a camera can enable its own window while the global one is off —
or set `enabled: false` to opt out while the global one is on.

A night camera builds its own video at **its** dawn, scoped to just that camera;
the others build on the normal nightly timer. All of this is editable from the
Config page under each camera's **Capture schedule**.

Note that covering the dark hours roughly **doubles** how long that camera is
capturing versus daylight-only. `interval_seconds` on the camera is the simplest
way to hold disk use flat — a night camera at 300s writes a fifth as many frames
per hour as one at 60s. See
[Storage estimates](Storage-and-Performance#storage-estimates).

### Cameras that sit out events

Not every camera can see the weather. One pointed indoors, at a doorway, or
framed tight on a walkway gains nothing from a storm — the burst frames are just
disk, and the event clip is a video of nothing happening. Set `events_enabled:
false` and that camera ignores events entirely:

```yaml
cameras:
  - name: front-door
    # ...host, credentials, etc...
    events_enabled: false     # default true; omit to participate normally
```

Two things change for that camera, and nothing else does:

- **No burst capture.** It holds its own `interval_seconds` through a storm
  instead of dropping to `events.burst_interval_seconds`. The other cameras
  still burst — the interval is resolved per camera.
- **No event videos.** The events build skips it, so it produces no
  `<date>_<tag>.mp4` clips. Its daily and yearly videos are unaffected.

Its frames are **still tagged** either way. The tag is metadata embedded in each
JPEG, it costs nothing, and keeping it means the frame record stays complete for
searching and filtering later — it's the capture rate and the video build that
opt out, not the metadata.

Event clips built *before* you turned this off stay on disk and keep expiring on
the normal `events_video.retention_days` schedule — turning this off stops new
clips, it doesn't delete old ones. (Rebuilding one of those past dates with
`build_timelapse.py events --date ...` does drop that camera's clips for that
date from the Events list, since the rebuild replaces the whole day's entries.)

Editable from the Config page under each camera's **Weather events**.
