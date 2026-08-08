"""Forward-looking counterpart to events.py: what's worth filming in the next
week or so.

events.py answers "is something happening *right now*?" — it drives frame
tagging and burst capture. This module answers "is something coming?" and
feeds the web UI's Forecast tab. Nothing here touches capture; it is a
read-only view.

Two kinds of prediction live side by side, and the difference matters:

- Weather is *probabilistic*. A storm eight days out is a hint; the same storm
  tomorrow is close to a fact. We never invent a confidence score to express
  that. Instead we report what the APIs actually say — the forecast
  probability of precipitation, and whether the two independent sources agree —
  and let the UI render the uncertainty from those facts.
- Moon events are *deterministic*. Skyfield computes them from an ephemeris,
  so a full moon nine days out is exactly as certain as one tomorrow. They
  carry no probability and must not be drawn as if they had one.

Sources are the same ones events.py already uses, so a forecast storm means
the same thing a detected storm does — the thresholds are literally the same
constants (see `events.STORM_CAPE_MIN` and friends):

- Open-Meteo hourly forecast, 10 days. CAPE *is* available on the forecast
  endpoint (unlike the archive endpoint, where it is not), which is what lets
  us apply the identical CAPE+precipitation test to a future hour.
- NWS gridpoint forecast (US only), which reaches about 7 days. Used purely to
  corroborate: it is the second opinion, never the sole trigger.
"""

import datetime as dt
import logging
import threading
import time

import requests

import events
from common import tzinfo_for
from events import USER_AGENT, WMO_TAGS, _num

log = logging.getLogger("forecast")

# Open-Meteo serves 10 days of hourly data (verified: 240 hours, CAPE
# non-null throughout). NWS gridpoint forecasts run to roughly 7 days, so
# days beyond this are single-source and the UI says so.
MAX_FORECAST_DAYS = 10
DEFAULT_FORECAST_DAYS = 10

# Snowfall worth pointing a camera at, in cm for the whole local day. A dusting
# that melts on contact makes a dull timelapse; this is roughly "you can see it
# settling". Override with `events.forecast_snow_cm_min`.
SNOW_MIN_CM = 0.5

# The forecast updates hourly at most, and these are free services being asked
# nicely — so the browser hitting /api/forecast on every page load must not
# become a request to Open-Meteo on every page load.
CACHE_TTL_SECONDS = 30 * 60

# How long a cached payload keeps being served after a refresh *fails*. Free
# weather APIs hand out 502/503s and timeouts regularly (the same reality that
# motivated events.stale_grace_minutes), and a six-hour-old forecast is far
# more useful than an error card. Anything staler than this is dropped and the
# UI is told the forecast is unavailable rather than shown something wrong.
STALE_MAX_SECONDS = 12 * 3600

# {cache key: (fetched_at, payload)} guarded by _CACHE_LOCK — the Flask app is
# threaded, so two simultaneous page loads must not both hit the network.
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _clear_cache():
    """Drop cached forecasts (used by tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

def _thresholds(events_cfg):
    """Storm thresholds, config-overridable — the same values, read the same
    way, that events.open_meteo_tags uses for current conditions. Keeping this
    in one expression is the point: if a storm is worth a burst capture now, an
    identical forecast hour is worth flagging on the calendar."""
    return (
        _num(events_cfg.get("storm_cape_min"), events.STORM_CAPE_MIN),
        _num(events_cfg.get("storm_gust_kmh"), events.STORM_GUST_MIN),
        _num(events_cfg.get("storm_precip_mm"), events.STORM_PRECIP_MIN),
    )


def fetch_open_meteo(lat, lon, days, timeout=15):
    """Hourly forecast for `days` days, timestamps already in local time.

    Hourly rather than daily on purpose. The daily endpoint gives
    precipitation_sum and wind_gusts_10m_max, but those are whole-day
    aggregates — pairing a calm morning's peak CAPE with an unrelated evening
    shower would invent storms that no single hour supports. CAPE is also only
    offered hourly. Requiring the thresholds to be met *within one hour* is
    what makes this the same test events.py applies to one instant of now.
    """
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "weather_code,precipitation,precipitation_probability,"
                      "snowfall,wind_gusts_10m,cape",
            "forecast_days": days,
            "timezone": "auto",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _open_meteo_days(payload, events_cfg, now=None):
    """Open-Meteo hourly payload -> {local date: {...day summary...}}.

    `now` is the current local time at the forecast location. Hours before it
    are dropped: Open-Meteo's first day starts at 00:00, so without this a
    storm that already blew through at 3 AM would be listed as upcoming, and
    by late evening today would still be advertising this morning's weather.
    """
    cape_min, gust_min, precip_min = _thresholds(events_cfg)
    snow_min = _num(events_cfg.get("forecast_snow_cm_min"), SNOW_MIN_CM)

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    def series(name):
        # A field the model doesn't provide comes back null (or missing
        # entirely). Treat it as absent, never as a calm zero.
        return hourly.get(name) or [None] * len(times)

    codes = series("weather_code")
    precip = series("precipitation")
    pops = series("precipitation_probability")
    snowfall = series("snowfall")
    gusts = series("wind_gusts_10m")
    capes = series("cape")

    days = {}
    for i, stamp in enumerate(times):
        try:
            when = dt.datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if now is not None and when < now:
            continue        # already happened — this is a forecast, not a log
        day = days.setdefault(when.date(), {
            "storm_hours": [], "storm_reasons": [], "storm_pops": [],
            "snow_cm": 0.0, "snow_hours": [], "snow_pops": [],
            "peak_cape": 0.0, "peak_gust": 0.0,
        })

        code = codes[i]
        hour_precip = _num(precip[i], 0.0)
        hour_gust = _num(gusts[i], 0.0)
        hour_cape = _num(capes[i], 0.0)
        hour_snow = _num(snowfall[i], 0.0)
        pop = pops[i]

        day["peak_cape"] = max(day["peak_cape"], hour_cape)
        day["peak_gust"] = max(day["peak_gust"], hour_gust)
        day["snow_cm"] += hour_snow

        storm = False
        if code in WMO_TAGS["storm"]:
            storm = True
            day["storm_reasons"].append(f"thunderstorm in the forecast (WMO {code})")
        # Rain actually falling, with the instability to drive convection.
        # CAPE alone is only potential — it sits high under clear skies — so
        # exactly as in events.py it never qualifies on its own.
        if hour_precip >= precip_min and hour_cape >= cape_min:
            storm = True
            day["storm_reasons"].append(
                f"{hour_cape:.0f} J/kg CAPE with {hour_precip:.1f} mm rain")
        # A gust front is worth filming wet or dry.
        if hour_gust >= gust_min:
            storm = True
            day["storm_reasons"].append(f"{hour_gust:.0f} km/h gusts")

        if storm:
            day["storm_hours"].append(when)
            if pop is not None:
                day["storm_pops"].append(int(pop))

        if hour_snow > 0 or code in WMO_TAGS["snow"]:
            day["snow_hours"].append(when)
            if pop is not None:
                day["snow_pops"].append(int(pop))

    # Trailing pass: a day only counts as snowy if enough actually accumulates.
    for day in days.values():
        if day["snow_cm"] < snow_min:
            day["snow_hours"] = []
            day["snow_pops"] = []
    return days


# ---------------------------------------------------------------------------
# NWS (US only) — corroboration, never the sole trigger
# ---------------------------------------------------------------------------

def _nws_forecast_url(lat, lon, timeout=10):
    """The gridpoint forecast URL for a location, cached in events._NET_CACHE
    alongside the observation-station lookup — the grid a point falls in never
    moves, so this is one request per location per process."""
    key = ("forecast_url", round(lat, 3), round(lon, 3))
    if key in events._NET_CACHE:
        return events._NET_CACHE[key]
    resp = requests.get(
        f"https://api.weather.gov/points/{lat},{lon}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    url = (resp.json().get("properties") or {}).get("forecast")
    if not url:
        raise RuntimeError("NWS returned no forecast URL for this point")
    events._NET_CACHE[key] = url
    return url


def fetch_nws(lat, lon, timeout=15):
    """NWS day/night forecast periods, or None outside the US.

    api.weather.gov answers 404 for any point it has no data for — every
    non-US location, which is expected rather than a fault. Returning None
    lets the caller say "one source here" instead of "something broke".
    """
    try:
        url = _nws_forecast_url(lat, lon, timeout)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.debug("NWS has no data for %.3f, %.3f (non-US?)", lat, lon)
            return None
        raise
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=timeout)
    resp.raise_for_status()
    return (resp.json().get("properties") or {}).get("periods") or []


def _nws_days(periods):
    """NWS periods -> {local date: {tag: {"pop": int|None, "text": str}}}.

    Periods are day/night halves, not days, so two can land on one date; we
    keep the wettest reading for each tag.
    """
    days = {}
    for period in periods or []:
        start = period.get("startTime")
        if not start:
            continue
        try:
            date = dt.datetime.fromisoformat(start).date()
        except ValueError:
            continue
        text = str(period.get("shortForecast") or "")
        pop = (period.get("probabilityOfPrecipitation") or {}).get("value")
        pop = int(pop) if pop is not None else None
        lowered = text.lower()
        for substring, tag in events.NWS_TAG_MAP:
            if tag not in ("storm", "snow") or substring not in lowered:
                continue
            entry = days.setdefault(date, {}).setdefault(tag, {"pop": None, "text": text})
            if pop is not None and (entry["pop"] is None or pop > entry["pop"]):
                entry["pop"] = pop
                entry["text"] = text
    return days


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _window_label(hours):
    """['2026-08-10T14:00', ...] -> "2 PM – 7 PM" for the UI."""
    if not hours:
        return None

    def fmt(when):
        hour = when.hour % 12 or 12
        return f"{hour} {'AM' if when.hour < 12 else 'PM'}"

    first, last = min(hours), max(hours)
    if first.hour == last.hour:
        return fmt(first)
    return f"{fmt(first)} – {fmt(last)}"


def _weather_event(kind, om_day, nws_entry, nws_covers_day):
    """One weather event for a day, or None if neither source flags it.

    `agreement` is a statement of fact about the sources — not a score we made
    up. The UI turns it into visual weight; this function never does.
    """
    om_hours = om_day["storm_hours"] if kind == "storm" else om_day["snow_hours"]
    om_hit = bool(om_hours)
    nws_hit = nws_entry is not None
    if not om_hit and not nws_hit:
        return None

    sources = ([{"name": "open-meteo"}] if om_hit else []) + \
              ([{"name": "nws"}] if nws_hit else [])
    if not nws_covers_day:
        agreement = "single-source"       # NWS has no data this far out / not US
    elif om_hit and nws_hit:
        agreement = "both"
    elif om_hit:
        agreement = "open-meteo-only"
    else:
        agreement = "nws-only"

    om_pops = om_day["storm_pops"] if kind == "storm" else om_day["snow_pops"]
    pops = list(om_pops)
    if nws_entry and nws_entry.get("pop") is not None:
        pops.append(nws_entry["pop"])

    if kind == "storm":
        reasons = list(dict.fromkeys(om_day["storm_reasons"]))[:3]
        detail = "; ".join(reasons)
        headline = "Thunderstorms"
    else:
        detail = f"{om_day['snow_cm']:.1f} cm expected" if om_day["snow_cm"] else ""
        headline = "Snow"
    if nws_entry:
        nws_text = nws_entry["text"]
        detail = f"{detail} · NWS: {nws_text}" if detail else f"NWS: {nws_text}"

    return {
        "kind": kind,
        "certain": False,
        "headline": headline,
        "detail": detail,
        # The APIs' own number, not ours. None when neither source gave one.
        "probability": max(pops) if pops else None,
        "agreement": agreement,
        "sources": [s["name"] for s in sources],
        "window": _window_label(om_hours),
        # Only meaningful when Open-Meteo is what flagged the day — quoting a
        # peak CAPE next to an NWS-only entry would imply Open-Meteo agreed.
        "peak_cape": round(om_day["peak_cape"]) if (kind == "storm" and om_hit) else None,
        "peak_gust": round(om_day["peak_gust"]) if (kind == "storm" and om_hit) else None,
    }


# Moon tags that are worth planning a shoot around, in the order we'd rank
# them. events.moon_tags returns the reason text already written for humans.
MOON_PRIORITY = ["blood-moon", "lunar-eclipse", "blue-moon", "harvest-moon", "full-moon"]


def _moon_events(date, cache_dir, lat, lon):
    """Deterministic lunar events for one future date.

    Reuses events.moon_tags unchanged — it takes an arbitrary date and is pure
    Skyfield arithmetic, so "what is the moon doing on day 9" costs nothing
    extra and is exact.
    """
    try:
        tags = events.moon_tags(date, cache_dir, lat, lon)
    except Exception as exc:
        log.warning("moon events for %s failed: %s", date, exc)
        return []
    out = []
    for tag in MOON_PRIORITY:
        if tag in tags:
            out.append({
                "kind": tag,
                "certain": True,          # computed, not predicted
                "headline": tags[tag][:1].upper() + tags[tag][1:],
                "detail": "",
                "probability": None,
                "agreement": None,
                "sources": ["skyfield"],
                "window": None,
            })
    return out


def _next_full_moon_beyond(last_date, cache_dir):
    """The first full moon after the forecast window, so a window with no moon
    event still tells you when the next one is instead of showing nothing."""
    try:
        candidates = events.full_moon_dates(last_date.year, cache_dir)
        candidates += events.full_moon_dates(last_date.year + 1, cache_dir)
    except Exception as exc:
        log.warning("next full moon lookup failed: %s", exc)
        return None
    for date in sorted(candidates):
        if date > last_date:
            return {"date": date.isoformat(), "days_away": (date - last_date).days}
    return None


def build_forecast(cfg, cache_dir, days=DEFAULT_FORECAST_DAYS):
    """Assemble the upcoming-events payload. Never raises for a source
    failure — a dead API becomes an entry in `sources` and a warning, so the
    tab degrades to "moon events only" rather than to an error page."""
    days = max(1, min(int(days), MAX_FORECAST_DAYS))
    events_cfg = cfg.get("events") or {}
    warnings = []
    sources = {}

    # Honour the same two switches capture does (see events.get_active_tags).
    # Forecasting is a different feature from tagging, but the flags mean "do
    # not go and fetch this" — weather_enabled: false must not still send the
    # user's coordinates to Open-Meteo and NWS whenever someone opens the tab,
    # and lunar_enabled: false must not trigger the ~17 MB ephemeris download.
    weather_on = bool(events_cfg.get("weather_enabled"))
    lunar_on = bool(events_cfg.get("lunar_enabled"))

    lat = lon = None
    if not weather_on:
        warnings.append(
            "Weather tagging is off, so storms and snow aren't forecast — turn on "
            "\"Storm/snow/rain tagging\" on the Config page to see them here.")
    elif events.has_location_configured(events_cfg):
        try:
            lat, lon = events.resolve_location(events_cfg)
        except Exception as exc:
            warnings.append(f"Could not resolve the configured location: {exc}. "
                            "Weather forecasting is unavailable; moon events are "
                            "unaffected.")
    else:
        warnings.append(
            "No location is set, so storms and snow can't be forecast — add "
            "events.zip or events.latitude/longitude on the Config page. Moon "
            "events need no location and are shown below.")

    om_days, nws_by_date, nws_covered = {}, {}, set()
    tzname = None

    if lat is not None:
        try:
            payload = fetch_open_meteo(lat, lon, days)
            tzname = payload.get("timezone")
            # Open-Meteo's hourly timestamps are naive local time for the
            # location (timezone=auto), so "now" must be naive local time in
            # that same zone to compare against them — a host in another zone
            # would otherwise drop or keep the wrong hours.
            local_now = dt.datetime.now(tzinfo_for(tzname)).replace(tzinfo=None)
            om_days = _open_meteo_days(payload, events_cfg, now=local_now)
            sources["open-meteo"] = {"ok": True}
        except Exception as exc:
            log.warning("Open-Meteo forecast failed: %s", exc)
            sources["open-meteo"] = {"ok": False, "error": str(exc)}
            warnings.append(f"Open-Meteo is not responding ({exc}). Storm and "
                            "snow forecasts are missing or incomplete.")
        try:
            periods = fetch_nws(lat, lon)
            if periods is None:
                sources["nws"] = {"ok": True, "available": False,
                                  "note": "NWS covers US locations only"}
            else:
                nws_by_date = _nws_days(periods)
                nws_covered = {dt.datetime.fromisoformat(p["startTime"]).date()
                               for p in periods if p.get("startTime")}
                sources["nws"] = {"ok": True, "available": True}
        except Exception as exc:
            log.warning("NWS forecast failed: %s", exc)
            sources["nws"] = {"ok": False, "available": False, "error": str(exc)}

    # The calendar the *cameras* are on. A host clock in another zone must not
    # shift the forecast a day relative to the days the Events tab shows.
    # Resolved once — resolve_timezone can itself hit the network.
    zone = tzname or events.resolve_timezone(cfg)
    today = dt.datetime.now(tzinfo_for(zone)).date()
    out_days = []
    for offset in range(days):
        date = today + dt.timedelta(days=offset)
        day_events = []

        om_day = om_days.get(date)
        if om_day:
            nws_day = nws_by_date.get(date) or {}
            covers = date in nws_covered
            for kind in ("storm", "snow"):
                event = _weather_event(kind, om_day, nws_day.get(kind), covers)
                if event:
                    day_events.append(event)

        if lunar_on:
            day_events += _moon_events(date, cache_dir, lat, lon)

        out_days.append({
            "date": date.isoformat(),
            "lead_days": offset,
            # Days past NWS's ~7-day reach have only one model behind them.
            # Surfaced as a fact so the UI can mark them without guessing.
            "nws_covered": date in nws_covered,
            "events": day_events,
        })

    last_date = today + dt.timedelta(days=days - 1)
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "timezone": zone,
        "days": out_days,
        "sources": sources,
        "warnings": warnings,
        "has_location": lat is not None,
        "next_full_moon": _next_full_moon_beyond(last_date, cache_dir) if lunar_on else None,
        "stale": False,
    }


def _weather_usable(payload) -> bool:
    """Did we actually get a weather forecast, as opposed to an empty one?

    This is the distinction that matters for caching. build_forecast never
    raises on a source failure — it returns a well-formed payload with no
    storms in it. That shape is indistinguishable from a genuinely calm ten
    days, so caching or displaying it during an Open-Meteo outage would quietly
    tell you "nothing coming" when the truth is "we couldn't ask". Same reason
    capture.py holds a source's last good tags instead of reading a timeout as
    all-clear.

    A location-less config has no weather to fetch and nothing to be stale
    about, so it counts as usable.
    """
    if not payload.get("has_location"):
        return True
    return bool((payload.get("sources", {}).get("open-meteo") or {}).get("ok"))


def get_forecast(cfg, cache_dir, days=DEFAULT_FORECAST_DAYS, force=False):
    """Cached build_forecast.

    Two things this protects against, both of which the free weather APIs make
    routine: hammering them on every page load, and letting a 502 masquerade as
    good news. A refresh that comes back without weather is discarded in favour
    of the last good payload, flagged `stale` so the UI can say how old it is.
    """
    ecfg = cfg.get("events") or {}
    key = (round(_num(ecfg.get("latitude"), 0.0), 3),
           round(_num(ecfg.get("longitude"), 0.0), 3),
           str(ecfg.get("zip") or ecfg.get("zip_code") or ""), int(days))
    now = time.time()

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached and not force and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    def serve_stale():
        """Last good payload, aged and labelled — or None if there isn't one."""
        if not cached or now - cached[0] >= STALE_MAX_SECONDS:
            return None
        stale = dict(cached[1])
        stale["stale"] = True
        stale["stale_minutes"] = int((now - cached[0]) / 60)
        return stale

    try:
        payload = build_forecast(cfg, cache_dir, days)
    except Exception as exc:
        log.warning("forecast build failed: %s", exc)
        fallback = serve_stale()
        if fallback is None:
            raise
        return fallback

    if not _weather_usable(payload):
        # Don't cache it either — a 30-minute TTL on an all-clear built during
        # an outage would outlive the outage itself.
        fallback = serve_stale()
        if fallback is not None:
            log.info("weather sources down; serving forecast from %s minutes ago",
                     fallback["stale_minutes"])
            return fallback

    with _CACHE_LOCK:
        _CACHE[key] = (now, payload)
    return payload


if __name__ == "__main__":
    # Quick check against the real APIs:
    #   forecast.py <lat> <lon> [days]
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("usage: forecast.py <lat> <lon> [days]")
    cfg = {"events": {"latitude": float(args[0]), "longitude": float(args[1]),
                      "lunar_enabled": True}}
    n = int(args[2]) if len(args) > 2 else DEFAULT_FORECAST_DAYS
    print(json.dumps(build_forecast(cfg, events.APP_ROOT / ".ephemeris", n), indent=2))
