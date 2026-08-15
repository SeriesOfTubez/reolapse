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

## Surviving API outages

These free services hand out timeouts and 502/503s fairly regularly. A failed
poll tells you nothing about the weather, so ReoLapse holds a source's last
known tags for `events.stale_grace_minutes` (default: three polls) instead of
reading the outage as *all clear*. Without this, one timed-out request in the
middle of a storm ends the burst early and truncates the event clip.
