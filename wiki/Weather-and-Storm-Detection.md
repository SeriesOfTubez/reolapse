# Weather & Storm Detection

`events.weather_enabled` and `events.lunar_enabled` are independent
switches — turn on either, both, or neither.

With `events.weather_enabled: true` and a location set (`events.zip`, or
`latitude`/`longitude`), capture polls NWS + Open-Meteo every `poll_minutes`
for storm/snow/rain conditions. Active tags are appended to
`data/conditions/<date>.jsonl` and embedded in each frame as a JPEG comment
(`{"tags":["storm"]}`, visible in exiftool). Storms/snow trigger burst
capture, and the nightly build renders a clip per event span into the
**Events** tab. Deflicker for these clips is off by default (it would smooth
away lightning flashes) but is fully configurable — see
`events_video.deflicker_size` / `deflicker_by_tag` in the
[Configuration Reference](Configuration-Reference) if you'd like it on for snow,
which has no lightning to protect. If weather tagging is enabled without a
resolvable location, storm/snow tagging is skipped and the web UI shows a
warning banner.

## Event frames in the daily video

While a storm or snow burst is active, capture drops to
`events.burst_interval_seconds`, and those minutes land in the same
`data/snapshots/<camera>/<date>/` folder as everything else — so a storm hour
is far denser than the rest of the day, and the daily video crawls through it
at a fraction of its normal pace.

`daily_video.include_events` (default `false`) excludes frames captured
inside an active burst span from the daily `.mp4` **only**. The event clip on
the Events tab, the yearly video's frame archive, and the frames on disk are
all untouched — this setting affects nothing but which frames make it into
that one video. Exclusion is driven by `events.burst_tags` specifically (not
`events_video.tags`, which is a separate, independent list of what gets its
own clip and can legitimately include non-burst tags like moon phases), and
spans are reconstructed from `data/conditions/<date>.jsonl` with the same,
configurable gap merge (`events_video.gap_minutes`) the event clips use — so
a frame is excluded from the daily video exactly when it's inside the span
that produced an event clip, never more or less. It's skipped entirely for
`events_enabled: false`
cameras: they never burst, so during a site-wide storm their frames are
ordinary cadence frames, and excluding them would gouge a hole in an
unaffected camera's day.

Any camera can override the global setting with `include_events_in_daily` —
see the [Configuration Reference](Configuration-Reference). If a day ends up
almost entirely inside a burst span, the daily video for that day is skipped
rather than rendered nearly empty (the yearly archive still gets that day's
frames regardless). Changed your mind after the fact? Rebuild it with
`build_timelapse.py daily --date YYYY-MM-DD --camera X` — that only works
while the day's snapshot frames still exist, i.e. within
`storage.keep_snapshots_days`.

## How a storm is detected

Weather services report "thunderstorm" far less often than thunderstorms
happen. Open-Meteo's `weather_code` only returns the thunderstorm values
(WMO 95/96/99) in a minority of actual storms — a storm downpour normally comes
back as *rain showers* instead. Detecting storms from that code alone means the
`storm` tag almost never fires, so no burst capture and no event clips. A real
deployment ran 19 days through repeated storms and logged **zero** storm tags
for exactly this reason.

So `storm` is corroborated from four independent signals, any one of which is
enough:

| signal | source | why |
|---|---|---|
| An active severe alert | NWS alerts (US) | officially warned events |
| Observed thunderstorm | nearest NWS station (US) | what's actually happening now, not a forecast |
| WMO code 95 / 96 / 99 | Open-Meteo | when the model does say thunderstorm |
| Rain **plus** high CAPE | Open-Meteo | convective rain the code labels as showers |
| Strong wind gusts | Open-Meteo | squall / gust front, wet or dry |

CAPE (convective available potential energy) is *potential*, not occurrence — it
can sit above 2000 J/kg under a clear sky — so it only ever tags a storm
alongside actual falling rain. Tune the thresholds with `events.storm_cape_min`,
`storm_precip_mm`, and `storm_gust_kmh`, or from the Config page under
**Storm detection tuning**.

Replayed against 92 days of real hourly weather, this raised storm detection
from 5 days to 17, with 5 dry-hour triggers out of 1588 (all of them genuine
gust fronts).

## How snow is detected

| signal | source | why |
|---|---|---|
| An active severe alert | NWS alerts (US) | officially warned events (blizzard, winter storm, etc.) |
| Observed snow/sleet/ice pellets | nearest NWS station (US) | what's actually happening now, not a forecast |
| WMO code 71/73/75/77/85/86 | Open-Meteo | when the model reports a snow code |
| Any measurable snowfall | Open-Meteo | catches snow reported with no snow code — light or mixed precipitation |

The last row is gated by `events.snow_cm_min` (default `0.0`, cm in the
reporting interval — the same "in the interval" phrasing as
`storm_precip_mm`, and **not** the same unit as `forecast_snow_cm_min`, which
accumulates across a whole forecast day): the tag fires only once the
snowfall rate clears the threshold, unless the field is missing from
Open-Meteo's response entirely, in which case detection falls back to the
snow-code row above and ignores the threshold rather than reading a missing
value as "no snow." At the default `0.0` this is behavior-compatible with
just checking the code, plus catching measurable snowfall reported with no
code at all — the same class of gap the storm corroboration above closes.
NWS alerts and station observations are keyword matches and stay
unthresholded regardless of this setting, same as the storm alert/observed
rows above.

Both the storm and snow thresholds are exposed on the Config page as
**Storm detection tuning** / **Snow detection tuning** sliders with
Conservative / Balanced / Aggressive presets — dragging one writes concrete
numbers into the same config keys described here (no separate "preset"
setting is stored), and a hand-edited value that doesn't exactly match a
tier shows as "Custom." Each tier carries a one-line note in the UI describing
what it trades away, since the name and the raw numbers alone don't convey it:

| Tier | Storm | Snow |
|---|---|---|
| Conservative | only severe storms; few false alarms, but pop-up storms will be missed | only real accumulation; flurries ignored |
| Balanced | catches most real storms without tagging ordinary rain | any measurable snowfall in the reporting interval |
| Aggressive | catches weak and pop-up storms; expect some heavy showers tagged as storms | any snow at all, including a reported trace |

Worth remembering when a storm you clearly remember doesn't appear in the
Events tab: at **Balanced**, a thunderstorm that never reaches CAPE 800 J/kg or
60 km/h gusts, with no NWS alert and the station reporting plain rain, is tagged
`rain` — and `rain` is in neither `events.burst_tags` nor `events_video.tags` by
default, so it produces no burst and no clip.

## Surviving API outages

These free services hand out timeouts and 502/503s fairly regularly. A failed
poll tells you nothing about the weather, so ReoLapse holds a source's last
known tags for `events.stale_grace_minutes` (default: three polls) instead of
reading the outage as *all clear*. Without this, one timed-out request in the
middle of a storm ends the burst early and truncates the event clip.

## The conditions log is the only record

Event clips are reconstructed from `data/conditions/<date>.jsonl` — not from
the tags embedded in the frames. If a day has no entry for a tag, no clip can
be built for it afterwards, even though the frames themselves are tagged
correctly. That makes the log worth protecting:

- A tag change is only marked as recorded **after** its line is written. It
  used to be marked first, so a failed write — a full disk — lost the change
  permanently: the next poll saw nothing new and never retried, and a storm
  could begin and end with nothing logged.
- A **failure while updating conditions can no longer take capture down**.
  Previously an out-of-space error while writing this log propagated out of the
  capture loop and killed the process; systemd restarted it, it re-detected the
  same change, and crashed again. Frames are worth more than tags, so the whole
  conditions refresh is now contained.
- A **half-written line is skipped** rather than raising. One truncated line —
  exactly what an interrupted append leaves behind — used to take out event
  building for that entire date.

If a day's log is missing or truncated anyway, its frames are still tagged and
still appear in the daily video; only the separate event *clip* is
unrecoverable.
