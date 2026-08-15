"""Detect notable weather/astronomy conditions for frame tagging and burst capture.

Sources (all free, no API keys):
- NWS active alerts (api.weather.gov, US only) — officially warned events
- Open-Meteo current conditions — catches storms/snow with no official alert
- Skyfield (JPL ephemeris) for moon events: full / blue / harvest moon plus
  blood moon (total lunar eclipse) and partial lunar eclipse. Computed locally;
  the ephemeris file (de421.bsp, ~17 MB) downloads once on first use.

Two independent switches in config (`events.weather_enabled`,
`events.lunar_enabled`) control these separately:
- Weather tagging needs a location (`zip` or `latitude`/`longitude`) — NWS and
  Open-Meteo are location-scoped APIs.
- Lunar phase tags (full/blue/harvest moon) are geocentric — the moon is full
  at the same instant everywhere on Earth — so no location is needed.
- Lunar *eclipses* are also geocentric events, but whether one is actually
  visible depends on whether the Moon is above your horizon at the time —
  which depends on where you are, hemisphere included. When a location is
  configured we only tag an eclipse if the Moon was up for it; without a
  location we can't check, so we tag every eclipse and say so in the reason.
"""

import datetime as dt
import logging
from pathlib import Path

import requests

log = logging.getLogger("events")

USER_AGENT = "reolink-timelapse-homelab (personal hobby project)"

APP_ROOT = Path(__file__).resolve().parent

# Skyfield objects and per-year event lists are cached here so the ephemeris
# loads once and events compute once per year, not on every poll.
_SKY = {}

# Network lookups that never change for a given location (e.g. which NWS station
# is nearest), so we don't re-resolve them on every poll.
_NET_CACHE = {}


def _num(value, default=0.0) -> float:
    """Coerce an API/config value to float; None and junk fall back.

    Open-Meteo returns null for a field a model doesn't provide (CAPE is absent
    in some regions), and a null must not read as zero-and-therefore-calm.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# (substring of NWS event name, tag)
NWS_TAG_MAP = [
    ("tornado", "storm"),
    ("thunderstorm", "storm"),
    ("hurricane", "storm"),
    ("tropical storm", "storm"),
    ("blizzard", "snow"),
    ("winter storm", "snow"),
    ("winter weather", "snow"),
    ("ice storm", "snow"),
    ("snow", "snow"),
    ("flood", "rain"),
]

# Substrings of NWS observed present-weather / textDescription. These come from
# real station observations rather than a forecast model, so a thunderstorm here
# means one is actually happening at the nearest ASOS site.
NWS_OBSERVED_MAP = [
    ("thunderstorm", "storm"),
    ("squall", "storm"),
    ("funnel", "storm"),
    ("snow", "snow"),
    ("sleet", "snow"),
    ("ice pellets", "snow"),
    ("freezing", "snow"),
    ("blizzard", "snow"),
    ("rain", "rain"),
    ("drizzle", "rain"),
]

# WMO weather codes from Open-Meteo's "weather_code"
WMO_TAGS = {
    "storm": {95, 96, 99},
    "snow": {71, 73, 75, 77, 85, 86},
    "rain": {61, 63, 65, 66, 67, 80, 81, 82},
}

# Open-Meteo's `current.weather_code` reports thunderstorm (95/96/99) far less
# often than thunderstorms actually occur — in practice a storm downpour comes
# back as 80-82 ("rain showers", 82 = violent) or 61-65 ("rain"). Relying on the
# code alone means `storm` effectively never fires, which is exactly what a real
# deployment showed: 12 rainy days, zero storm tags, so no burst capture and no
# event videos. So we corroborate with the physical fields instead.
#
# CAPE is convective *potential*, not occurrence — it can sit above 2000 J/kg
# under a clear sky — so it only counts alongside actual falling precipitation.
STORM_CAPE_MIN = 800.0       # J/kg, with precipitation: convective rain
STORM_GUST_MIN = 60.0        # km/h, on its own: squall / downburst
STORM_PRECIP_MIN = 0.1       # mm in the reporting interval = "actually raining"

# cm in the reporting interval — deliberately the same "mm/cm in the interval"
# phrasing as STORM_PRECIP_MIN, and NOT the same unit as forecast_snow_cm_min
# (accumulated cm across a whole forecast day). At the default 0.0 this is
# behaviour-compatible with the code-only rule below; raising it makes live
# snow tagging less sensitive.
SNOW_CM_MIN = 0.0


def _skyfield(cache_dir):
    """Lazily load the timescale + ephemeris, cached for the process."""
    if "eph" not in _SKY:
        from skyfield.api import Loader
        loader = Loader(str(cache_dir))
        _SKY["ts"] = loader.timescale()
        _SKY["eph"] = loader("de421.bsp")
    return _SKY["ts"], _SKY["eph"]


def _local_date(t):
    """Skyfield Time -> local calendar date (system timezone)."""
    return t.utc_datetime().astimezone().date()


def full_moon_dates(year, cache_dir):
    """Local dates of every full moon in a calendar year (cached per year)."""
    key = ("full", year)
    if key not in _SKY:
        from skyfield import almanac
        ts, eph = _skyfield(cache_dir)
        times, phases = almanac.find_discrete(
            ts.utc(year, 1, 1), ts.utc(year + 1, 1, 2), almanac.moon_phases(eph))
        _SKY[key] = sorted(_local_date(t) for t, p in zip(times, phases) if p == 2)
    return _SKY[key]


def _autumn_equinox(year, cache_dir):
    key = ("equinox", year)
    if key not in _SKY:
        from skyfield import almanac
        ts, eph = _skyfield(cache_dir)
        times, seasons = almanac.find_discrete(
            ts.utc(year, 9, 1), ts.utc(year, 10, 1), almanac.seasons(eph))
        found = [_local_date(t) for t, e in zip(times, seasons) if e == 2]
        _SKY[key] = found[0] if found else dt.date(year, 9, 22)
    return _SKY[key]


# almanac.seasons() event codes: 0 = March equinox, 1 = June solstice,
# 2 = September equinox, 3 = December solstice. Which season each one STARTS
# depends on hemisphere — the event itself is the same instant everywhere.
NORTHERN_SEASON_NAMES = {0: "spring", 1: "summer", 2: "fall", 3: "winter"}
SOUTHERN_SEASON_NAMES = {0: "fall", 1: "winter", 2: "spring", 3: "summer"}


def _season_boundaries(year, cache_dir):
    """[(event code, Skyfield Time), ...] for the four equinox/solstice
    instants that fall within a calendar year (cached per year)."""
    key = ("season_bounds", year)
    if key not in _SKY:
        from skyfield import almanac
        ts, eph = _skyfield(cache_dir)
        times, kinds = almanac.find_discrete(
            ts.utc(year, 1, 1), ts.utc(year + 1, 1, 1), almanac.seasons(eph))
        _SKY[key] = list(zip((int(k) for k in kinds), times))
    return _SKY[key]


def season_for_date(date, cache_dir, lat=None):
    """Which astronomical season `date` falls in, hemisphere-aware (defaults
    to Northern Hemisphere if `lat` is unavailable — the common case, and
    matches unset/no-location deployments).
    """
    bounds = (_season_boundaries(date.year - 1, cache_dir)
              + _season_boundaries(date.year, cache_dir))
    local_bounds = sorted((_local_date(t), kind) for kind, t in bounds)

    names = SOUTHERN_SEASON_NAMES if (lat is not None and lat < 0) else NORTHERN_SEASON_NAMES
    current = local_bounds[0][1]
    for boundary_date, kind in local_bounds:
        if boundary_date > date:
            break
        current = kind
    return names[current]


def sunrise_sunset(date, lat, lon, cache_dir, tz=None):
    """Local (sunrise, sunset) times as datetime.time for a given date and
    location. Either may be None during polar day/night, when the sun
    doesn't rise or set that day.

    `tz` (a tzinfo) selects the zone the returned times are expressed in and
    the zone `date` is interpreted in; None uses the host timezone.
    """
    from skyfield import almanac
    from skyfield.api import wgs84
    ts, eph = _skyfield(cache_dir)
    location = wgs84.latlon(lat, lon)
    # A full local day in UTC terms may span parts of two UTC dates, so scan
    # a day of padding on each side and keep only events that land on `date`
    # once converted back to local time.
    t0 = ts.utc(date.year, date.month, date.day - 1)
    t1 = ts.utc(date.year, date.month, date.day + 2)
    times, sun_is_up = almanac.find_discrete(t0, t1, almanac.sunrise_sunset(eph, location))

    sunrise = sunset = None
    for t, is_up in zip(times, sun_is_up):
        local = t.utc_datetime().astimezone(tz)
        if local.date() != date:
            continue
        if is_up and sunrise is None:
            sunrise = local.time()
        elif not is_up and sunset is None:
            sunset = local.time()
    return sunrise, sunset


def lunar_eclipses(year, cache_dir):
    """Local date -> (eclipse type code, Skyfield Time of greatest eclipse).

    Type codes: 0 penumbral, 1 partial, 2 total.
    """
    key = ("eclipse", year)
    if key not in _SKY:
        from skyfield import eclipselib
        ts, eph = _skyfield(cache_dir)
        times, codes, _ = eclipselib.lunar_eclipses(
            ts.utc(year, 1, 1), ts.utc(year + 1, 1, 1), eph)
        _SKY[key] = {_local_date(t): (int(c), t) for t, c in zip(times, codes)}
    return _SKY[key]


def _moon_is_up(t, lat, lon, cache_dir) -> bool:
    """Is the Moon above the horizon at time t, seen from (lat, lon)?"""
    from skyfield.api import wgs84
    ts, eph = _skyfield(cache_dir)
    observer = eph["earth"] + wgs84.latlon(lat, lon)
    alt, _, _ = observer.at(t).observe(eph["moon"]).apparent().altaz()
    return alt.degrees > 0


def moon_tags(today, cache_dir, lat=None, lon=None):
    """Moon-event tags for a given local date, computed via Skyfield.

    `lat`/`lon` are optional. When given, an eclipse is only tagged if the
    Moon was actually above the horizon at that location — otherwise it
    couldn't have been photographed there regardless of the eclipse type.
    Without a location, eclipses are tagged unconditionally (visibility
    unknown) since there's nothing to check them against.
    """
    tags = {}
    entry = lunar_eclipses(today.year, cache_dir).get(today)
    if entry:
        code, t = entry
        name = "total lunar eclipse" if code == 2 else "partial lunar eclipse"
        tag = "blood-moon" if code == 2 else "lunar-eclipse"
        if code in (1, 2):  # penumbral (0) is barely perceptible — not tagged
            if lat is None or lon is None:
                tags[tag] = f"{name} (visibility not checked — no location configured)"
            elif _moon_is_up(t, lat, lon, cache_dir):
                tags[tag] = f"{name}, visible from your location"
            # else: below the horizon here — nothing your camera could see

    fulls = full_moon_dates(today.year, cache_dir)
    if today in fulls:
        tags["full-moon"] = "full moon"
        month_fulls = [d for d in fulls if d.month == today.month]
        if len(month_fulls) == 2 and today == month_fulls[1]:
            tags["blue-moon"] = "second full moon this month"
        equinox = _autumn_equinox(today.year, cache_dir)
        if today == min(fulls, key=lambda d: abs((d - equinox).days)):
            tags["harvest-moon"] = "full moon nearest the autumn equinox"
    return tags


def nws_alert_tags(lat, lon, timeout=10) -> dict:
    tags = {}
    resp = requests.get(
        f"https://api.weather.gov/alerts/active?point={lat},{lon}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    for feature in resp.json().get("features", []):
        event = feature.get("properties", {}).get("event", "")
        for substring, tag in NWS_TAG_MAP:
            if substring in event.lower():
                tags.setdefault(tag, f"NWS: {event}")
    return tags


def open_meteo_tags(lat, lon, timeout=10, cfg=None) -> dict:
    """Current-conditions tags from Open-Meteo.

    Asks for the physical fields alongside the WMO code, because the code alone
    under-reports thunderstorms badly (see STORM_CAPE_MIN above), and (see
    SNOW_CM_MIN above) under-reports snow the same way — measurable snowfall
    with no 71/73/75 code currently produces no snow tag at all.
    """
    cfg = cfg or {}
    cape_min = _num(cfg.get("storm_cape_min"), STORM_CAPE_MIN)
    gust_min = _num(cfg.get("storm_gust_kmh"), STORM_GUST_MIN)
    precip_min = _num(cfg.get("storm_precip_mm"), STORM_PRECIP_MIN)
    snow_min = _num(cfg.get("snow_cm_min"), SNOW_CM_MIN)

    tags = {}
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon,
                "current": "weather_code,precipitation,wind_gusts_10m,cape,snowfall"},
        timeout=timeout,
    )
    resp.raise_for_status()
    current = resp.json()["current"]
    code = current.get("weather_code")
    precip = _num(current.get("precipitation"), 0.0)
    gusts = _num(current.get("wind_gusts_10m"), 0.0)
    cape = _num(current.get("cape"), 0.0)

    # storm/rain: unconditional per-code tagging, same as always. snow is
    # handled separately below — its tag is gated by snow_cm_min, so it can't
    # go through this same unconditional loop.
    for tag, codes in WMO_TAGS.items():
        if tag == "snow":
            continue
        if code in codes:
            tags.setdefault(tag, f"Open-Meteo weather code {code}")

    # Raining hard enough to see, with the instability to drive convection.
    if precip >= precip_min and cape >= cape_min:
        tags.setdefault("storm", f"Open-Meteo convective: {precip} mm precip, "
                                 f"CAPE {cape:.0f} J/kg")
    # A squall line's gust front is worth capturing whether or not it's raining.
    if gusts >= gust_min:
        tags.setdefault("storm", f"Open-Meteo wind gusts {gusts:.0f} km/h")

    # snow tag fires iff (a WMO snow code OR any measurable snowfall) AND the
    # snowfall rate clears snow_cm_min. `snowfall` absent from the response
    # (a model that doesn't provide it) is a "we don't know", not a "zero" —
    # _num(None, 0.0) collapsing that to 0.0 would silently fail the >=
    # snow_min check and disable the code-based trigger entirely, so that
    # case is handled explicitly, ignoring the threshold and falling back to
    # today's code-only rule instead.
    snowfall_raw = current.get("snowfall")
    if snowfall_raw is None:
        if code in WMO_TAGS["snow"]:
            tags.setdefault("snow", f"Open-Meteo weather code {code}")
    else:
        snowfall = _num(snowfall_raw, 0.0)
        if (code in WMO_TAGS["snow"] or snowfall > 0) and snowfall >= snow_min:
            tags.setdefault("snow", f"Open-Meteo snowfall {snowfall} cm")
    return tags


def _nws_station(lat, lon, timeout=10):
    """Nearest NWS observation station id, cached (it never moves)."""
    key = ("station", round(lat, 3), round(lon, 3))
    if key in _NET_CACHE:
        return _NET_CACHE[key]
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    point = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                         headers=headers, timeout=timeout)
    point.raise_for_status()
    stations_url = point.json()["properties"]["observationStations"]
    stations = requests.get(stations_url, headers=headers, timeout=timeout)
    stations.raise_for_status()
    features = stations.json().get("features") or []
    if not features:
        raise RuntimeError("no NWS observation station near this location")
    station = features[0]["properties"]["stationIdentifier"]
    _NET_CACHE[key] = station
    return station


def nws_observed_tags(lat, lon, timeout=10) -> dict:
    """Tags from the nearest NWS station's latest *observation* (US only).

    Alerts only fire for officially warned events, which misses ordinary
    thunderstorms entirely. This is what the station actually reports right now.
    """
    tags = {}
    station = _nws_station(lat, lon, timeout)
    resp = requests.get(
        f"https://api.weather.gov/stations/{station}/observations/latest",
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    props = resp.json().get("properties") or {}

    phrases = []
    for entry in props.get("presentWeather") or []:
        phrases.append(" ".join(str(entry.get(k) or "")
                                for k in ("intensity", "modifier", "weather")))
    if props.get("textDescription"):
        phrases.append(props["textDescription"])

    blob = " ".join(phrases).lower()
    for substring, tag in NWS_OBSERVED_MAP:
        if substring in blob:
            tags.setdefault(tag, f"NWS {station} observed: "
                                 f"{props.get('textDescription') or substring}")
    return tags


_ZIP_CACHE = {}


def has_location_configured(cfg) -> bool:
    """Cheap, network-free check: is a location present in config at all?"""
    if cfg.get("latitude") is not None and cfg.get("longitude") is not None:
        return True
    return bool(cfg.get("zip") or cfg.get("zip_code"))


def resolve_location(cfg) -> tuple:
    """(lat, lon) from config: explicit latitude/longitude, or a US ZIP resolved
    via Zippopotam.us (free, no key) and cached for the process."""
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    zip_code = cfg.get("zip") or cfg.get("zip_code")
    if not zip_code:
        raise RuntimeError("set events.zip or events.latitude/longitude in config")
    zip_code = str(zip_code).strip()
    if zip_code not in _ZIP_CACHE:
        resp = requests.get(f"https://api.zippopotam.us/us/{zip_code}",
                            headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        place = resp.json()["places"][0]
        _ZIP_CACHE[zip_code] = (float(place["latitude"]), float(place["longitude"]))
        log.info("resolved ZIP %s -> %.4f, %.4f", zip_code, *_ZIP_CACHE[zip_code])
    return _ZIP_CACHE[zip_code]


_TZ_CACHE = {}


def resolve_timezone(cfg):
    """IANA timezone name used for capture timing (e.g. 'America/Chicago').

    An explicit ``capture.timezone`` wins; otherwise the auto-detected zone
    (see detect_timezone); else None (caller uses the host timezone).
    """
    explicit = (cfg.get("capture") or {}).get("timezone")
    if explicit:
        return str(explicit).strip()
    return detect_timezone(cfg)


def detect_timezone(cfg):
    """Auto-detect the IANA timezone from the configured location
    (``events.zip`` / ``latitude``+``longitude``) via Open-Meteo's
    ``timezone=auto``, cached to ``<storage.root>/timezone.txt`` so it survives
    offline restarts. Ignores any explicit ``capture.timezone``. Returns None if
    no location is set or the lookup fails — all best-effort, never raises.
    """
    try:
        lat, lon = resolve_location(cfg.get("events") or {})
    except Exception:
        return None

    key = (round(lat, 3), round(lon, 3))
    if key in _TZ_CACHE:
        return _TZ_CACHE[key]

    cache_file = None
    try:
        cache_file = Path(cfg["storage"]["root"]) / "timezone.txt"
        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8").strip()
            if cached:
                _TZ_CACHE[key] = cached
                return cached
    except Exception:
        pass

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "timezone": "auto",
                    "forecast_days": 1},
            headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        tzname = resp.json().get("timezone")
    except Exception as exc:
        log.warning("timezone auto-detect failed (%s) — using the host timezone; "
                    "set capture.timezone to be sure", exc)
        return None

    if tzname:
        _TZ_CACHE[key] = tzname
        log.info("auto-detected timezone %s for %.3f, %.3f", tzname, lat, lon)
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(tzname, encoding="utf-8")
            except OSError:
                pass
    return tzname


def poll_sources(events_cfg, cache_dir=None) -> dict:
    """Poll every enabled source separately: {name: {"tags": {...}, "ok": bool}}.

    Keeping sources named and their success separate is what lets the caller
    tell "nothing is happening" apart from "we couldn't reach anyone" — a
    distinction get_active_tags alone can't express, since both look like an
    empty dict. Callers that poll on a timer should hold the last good result
    for a failed source rather than treating it as all-clear (see
    capture.Conditions), or one API timeout ends a burst mid-storm.
    """
    cache_dir = Path(cache_dir) if cache_dir else APP_ROOT / ".ephemeris"
    cache_dir.mkdir(parents=True, exist_ok=True)

    weather_on = bool(events_cfg.get("weather_enabled"))
    lunar_on = bool(events_cfg.get("lunar_enabled"))
    season_on = bool(events_cfg.get("season_enabled"))

    lat = lon = None
    if weather_on or lunar_on or season_on:
        try:
            lat, lon = resolve_location(events_cfg)
        except Exception as exc:
            if weather_on:
                log.warning("weather_enabled but location unresolved, "
                            "skipping storm/snow tagging: %s", exc)
            # lunar/season tagging proceed without lat/lon regardless

    sources = []
    if weather_on and lat is not None:
        sources += [
            ("nws-alerts", lambda: nws_alert_tags(lat, lon)),
            ("nws-observed", lambda: nws_observed_tags(lat, lon)),
            ("open-meteo", lambda: open_meteo_tags(lat, lon, cfg=events_cfg)),
        ]
    if lunar_on:
        sources.append(("lunar", lambda: moon_tags(dt.date.today(), cache_dir, lat, lon)))
    if season_on:
        sources.append(("season",
                        lambda: {season_for_date(dt.date.today(), cache_dir, lat):
                                 "astronomical season"}))

    results = {}
    for name, source in sources:
        try:
            results[name] = {"tags": dict(source()), "ok": True}
        except Exception as exc:
            # NWS observations are US-only; a non-US location 404s every poll,
            # which is expected rather than a fault worth shouting about.
            level = log.debug if name == "nws-observed" else log.warning
            level("event source %s failed: %s", name, exc)
            results[name] = {"tags": {}, "ok": False}
    return results


def get_active_tags(events_cfg, cache_dir=None) -> dict:
    """All currently active tags -> human-readable reason.

    `events.weather_enabled`, `events.lunar_enabled`, and `events.season_enabled`
    are independent: weather tagging needs a resolvable location, lunar phase
    tags don't (only eclipse-visibility checking benefits from one), and
    season tagging only uses location to pick the correct hemisphere (it
    defaults to Northern without one). Each source is independent; one
    failing never blocks the others.
    `cache_dir` stores the Skyfield ephemeris (downloaded once).
    """
    tags = {}
    for result in poll_sources(events_cfg, cache_dir).values():
        for tag, reason in result["tags"].items():
            tags.setdefault(tag, reason)
    for tag in events_cfg.get("force_tags") or []:  # testing hook
        tags.setdefault(tag, "forced via config")
    return tags


if __name__ == "__main__":
    # Quick sanity check: print this year's moon events and current tags.
    # Usage: events.py [lat lon] [year]
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    cache = APP_ROOT / ".ephemeris"
    args = sys.argv[1:]
    year = int(args[2]) if len(args) > 2 else dt.date.today().year
    print(f"Full moons {year}: {[str(d) for d in full_moon_dates(year, cache)]}")
    names = {0: "penumbral", 1: "partial", 2: "total"}
    ecl = {str(d): names[c] for d, (c, _) in lunar_eclipses(year, cache).items()}
    print(f"Lunar eclipses {year}: {ecl}")
    if len(args) >= 2:
        cfg = {"latitude": float(args[0]), "longitude": float(args[1]),
               "weather_enabled": True, "lunar_enabled": True}
        print(json.dumps(get_active_tags(cfg, cache), indent=2))
