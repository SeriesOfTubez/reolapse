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

# WMO weather codes from Open-Meteo's "weather_code"
WMO_TAGS = {
    "storm": {95, 96, 99},
    "snow": {71, 73, 75, 77, 85, 86},
    "rain": {61, 63, 65, 66, 67, 80, 81, 82},
}


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


def open_meteo_tags(lat, lon, timeout=10) -> dict:
    tags = {}
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon, "current": "weather_code"},
        timeout=timeout,
    )
    resp.raise_for_status()
    code = resp.json()["current"]["weather_code"]
    for tag, codes in WMO_TAGS.items():
        if code in codes:
            tags.setdefault(tag, f"Open-Meteo weather code {code}")
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
    """IANA timezone name for capture timing (e.g. 'America/Chicago'), or None.

    Priority: an explicit ``capture.timezone``; else auto-detected from the
    configured location (``events.zip`` / ``latitude``+``longitude``) via
    Open-Meteo's ``timezone=auto`` and cached to ``<storage.root>/timezone.txt``
    so it survives offline restarts; else None (caller uses the host timezone).

    Everything is best-effort — any failure returns None rather than raising, so
    capture never breaks over a timezone lookup.
    """
    explicit = (cfg.get("capture") or {}).get("timezone")
    if explicit:
        return str(explicit).strip()

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
        sources += [lambda: nws_alert_tags(lat, lon), lambda: open_meteo_tags(lat, lon)]
    if lunar_on:
        sources.append(lambda: moon_tags(dt.date.today(), cache_dir, lat, lon))
    if season_on:
        sources.append(lambda: {season_for_date(dt.date.today(), cache_dir, lat):
                                 "astronomical season"})

    tags = {}
    for source in sources:
        try:
            for tag, reason in source().items():
                tags.setdefault(tag, reason)
        except Exception as exc:
            log.warning("event source failed: %s", exc)
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
