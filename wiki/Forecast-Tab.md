# Forecast Tab (what's coming)

The Events tab is backward-looking: clips already cut from storms that
happened. The **Forecast** tab is its counterpart — the next 10 days of
storms, snow, and moon events, so you can plan around them.

It is read-only. It never changes what gets captured, never triggers a burst,
and writes nothing. It exists to answer "is anything worth watching for this
week?"

## It agrees with what actually gets tagged

A forecast storm means exactly what a detected storm means, because it's the
same test: the CAPE, gust, and precipitation thresholds under
[How a storm is detected](Weather-and-Storm-Detection#how-a-storm-is-detected)
are applied to forecast hours instead of the current hour. Retune those
thresholds and the Forecast tab retunes with them.

The check runs per *hour*, not per day. Open-Meteo also publishes daily
aggregates, but a day's peak instability and its heaviest rain can be twelve
hours apart, and pairing them would invent storms that no actual hour
supports.

## Confidence, and why there's no confidence score

Your day-8 storm is a maybe; tomorrow's is close to a fact. Rather than
compress that into a number we made up, the tab shows the things the
forecasts actually said:

- **Probability of precipitation**, straight from the APIs.
- **Whether the two sources agree.** Open-Meteo and the US National Weather
  Service are independent forecasts. "Both forecasts agree" is meaningfully
  stronger than "Open-Meteo only — the NWS forecast doesn't show it", and the
  tab says which it is.
- **How far out it is**, both in words ("in 3 days") and as the weight of the
  stripe down the left edge of each day.

NWS forecasts reach about 7 days; Open-Meteo reaches 10. Days 8-10 therefore
rest on a single model, and the tab marks them rather than letting them look
as solid as tomorrow. Outside the US, NWS has no data at all — Open-Meteo
carries the whole forecast and every day is marked single-source.

Moon events are handled differently on purpose. They're computed from an
ephemeris, not predicted, so a full moon nine days out is exactly as certain
as one tomorrow. They get no percentage, no probability bar, and no
"treat this as a heads-up" caveat — just a solid chip and the note that it's
calculated rather than forecast.

## Degrading gracefully

These are free services and they hand out 502s, 503s, and timeouts routinely.

- The forecast is cached for 30 minutes, so browsing the UI doesn't hammer
  them — a page load costs nothing.
- If a refresh fails, the last good forecast keeps being served, labelled with
  its age. This matters more than it sounds: a failed fetch produces a
  perfectly well-formed forecast with *no storms in it*, which is
  indistinguishable from a genuinely calm week. Serving that would quietly
  tell you "nothing coming" when the truth is "we couldn't ask" — the same
  trap `stale_grace_minutes` avoids for live capture.
- With no location configured, weather is skipped and the tab still shows moon
  events, with a note explaining what to set.

## Settings

Both live under `events:` in `config.yaml`, and both are editable from the
Config page:

| Key | Default | What it does |
| --- | --- | --- |
| `forecast_days` | `10` | How far ahead to look, 1-10. Set it to `7` to show only days both forecasts cover. |
| `forecast_snow_cm_min` | `0.5` | Snow across a day before it's worth showing. A dusting that melts on contact makes a dull timelapse. |

The storm thresholds are shared with live detection and documented under
[How a storm is detected](Weather-and-Storm-Detection#how-a-storm-is-detected).
