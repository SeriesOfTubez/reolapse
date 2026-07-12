"""Detect notable weather/astronomy conditions for frame tagging and burst capture.

Sources (all free, no API keys):
- NWS active alerts (api.weather.gov, US only) — officially warned events
- Open-Meteo current conditions — catches storms/snow with no official alert
- Skyfield (JPL ephemeris) for moon events: full / blue / harvest moon plus
  blood moon (total lunar eclipse) and partial lunar eclipse. Computed locally;
  the ephemeris file (de421.bsp, ~17 MB) downloads once on first use.
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
        times, events = almanac.find_discrete(
            ts.utc(year, 9, 1), ts.utc(year, 10, 1), almanac.seasons(eph))
        found = [_local_date(t) for t, e in zip(times, events) if e == 2]
        _SKY[key] = found[0] if found else dt.date(year, 9, 22)
    return _SKY[key]


def lunar_eclipses(year, cache_dir):
    """Local date -> eclipse type code (0 penumbral, 1 partial, 2 total)."""
    key = ("eclipse", year)
    if key not in _SKY:
        from skyfield import eclipselib
        ts, eph = _skyfield(cache_dir)
        times, codes, _ = eclipselib.lunar_eclipses(
            ts.utc(year, 1, 1), ts.utc(year + 1, 1, 1), eph)
        _SKY[key] = {_local_date(t): int(c) for t, c in zip(times, codes)}
    return _SKY[key]


def moon_tags(today, cache_dir):
    """Moon-event tags for a given local date, computed via Skyfield."""
    tags = {}
    eclipse = lunar_eclipses(today.year, cache_dir).get(today)
    if eclipse == 2:
        tags["blood-moon"] = "total lunar eclipse"
    elif eclipse == 1:
        tags["lunar-eclipse"] = "partial lunar eclipse"
    # penumbral (0) is barely perceptible — not tagged

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


def resolve_location(cfg) -> tuple:
    """(lat, lon) from config: explicit latitude/longitude, or a US ZIP resolved
    via Zippopotam.us (free, no key) and cached for the process."""
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    zip_code = cfg.get("zip") or cfg.get("zip_code")
    if not zip_code:
        raise RuntimeError("set weather.zip or weather.latitude/longitude in config")
    zip_code = str(zip_code).strip()
    if zip_code not in _ZIP_CACHE:
        resp = requests.get(f"https://api.zippopotam.us/us/{zip_code}",
                            headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        place = resp.json()["places"][0]
        _ZIP_CACHE[zip_code] = (float(place["latitude"]), float(place["longitude"]))
        log.info("resolved ZIP %s -> %.4f, %.4f", zip_code, *_ZIP_CACHE[zip_code])
    return _ZIP_CACHE[zip_code]


def get_active_tags(weather_cfg, cache_dir=None) -> dict:
    """All currently active tags -> human-readable reason.

    Each source is independent; one failing never blocks the others. Moon
    events need no location, so they run even if location lookup fails.
    `cache_dir` stores the Skyfield ephemeris (downloaded once).
    """
    cache_dir = Path(cache_dir) if cache_dir else APP_ROOT / ".ephemeris"
    cache_dir.mkdir(parents=True, exist_ok=True)

    sources = [lambda: moon_tags(dt.date.today(), cache_dir)]
    try:
        lat, lon = resolve_location(weather_cfg)
        sources = [lambda: nws_alert_tags(lat, lon),
                   lambda: open_meteo_tags(lat, lon)] + sources
    except Exception as exc:
        log.warning("location unavailable, weather tags disabled: %s", exc)

    tags = {}
    for source in sources:
        try:
            for tag, reason in source().items():
                tags.setdefault(tag, reason)
        except Exception as exc:
            log.warning("event source failed: %s", exc)
    for tag in weather_cfg.get("force_tags") or []:  # testing hook
        tags.setdefault(tag, "forced via config")
    return tags


if __name__ == "__main__":
    # Quick sanity check: print this year's moon events and current tags.
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    cache = APP_ROOT / ".ephemeris"
    year = int(sys.argv[3]) if len(sys.argv) > 3 else dt.date.today().year
    print(f"Full moons {year}: {[str(d) for d in full_moon_dates(year, cache)]}")
    names = {0: "penumbral", 1: "partial", 2: "total"}
    ecl = {str(d): names[c] for d, c in lunar_eclipses(year, cache).items()}
    print(f"Lunar eclipses {year}: {ecl}")
    if len(sys.argv) >= 3:
        cfg = {"latitude": float(sys.argv[1]), "longitude": float(sys.argv[2])}
        print(json.dumps(get_active_tags(cfg, cache), indent=2))
