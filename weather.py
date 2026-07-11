"""Detect notable weather/astronomy conditions for frame tagging and burst capture.

Sources (all free, no API keys):
- NWS active alerts (api.weather.gov, US only) — officially warned events
- Open-Meteo current conditions — catches storms/snow with no official alert
- Local computation for moon events (full / blue / harvest)

Blood moons (lunar eclipses) are not computed yet — they need an ephemeris or
eclipse table rather than phase math.
"""

import datetime as dt
import logging

import requests

log = logging.getLogger("weather")

USER_AGENT = "reolink-timelapse-homelab (personal hobby project)"

SYNODIC_DAYS = 29.530588853
# Reference new moon: 2000-01-06 18:14 UTC
NEW_MOON_EPOCH = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)

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


def moon_age(when_utc: dt.datetime) -> float:
    """Days since new moon (0..29.53)."""
    return ((when_utc - NEW_MOON_EPOCH).total_seconds() / 86400) % SYNODIC_DAYS


def full_moon_dates(year: int) -> list:
    """Dates (UTC-noon-based) of full moons in a year."""
    half = SYNODIC_DAYS / 2
    best = {}  # cycle index -> (deviation, date)
    day = dt.date(year, 1, 1)
    while day.year == year:
        noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=dt.timezone.utc)
        dev = abs(moon_age(noon) - half)
        if dev < 0.6:
            cycle = int((noon - NEW_MOON_EPOCH).total_seconds() / 86400 / SYNODIC_DAYS)
            if cycle not in best or dev < best[cycle][0]:
                best[cycle] = (dev, day)
        day += dt.timedelta(days=1)
    return sorted(d for _, d in best.values())


def moon_tags(today: dt.date) -> dict:
    tags = {}
    fulls = full_moon_dates(today.year)
    if today not in fulls:
        return tags
    tags["full-moon"] = "computed from lunar phase"
    month_fulls = [d for d in fulls if d.month == today.month]
    if len(month_fulls) == 2 and today == month_fulls[1]:
        tags["blue-moon"] = "second full moon this month"
    equinox = dt.date(today.year, 9, 22)
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


def get_active_tags(weather_cfg) -> dict:
    """All currently active tags -> human-readable reason.

    Each source is independent; one failing never blocks the others.
    """
    lat = weather_cfg["latitude"]
    lon = weather_cfg["longitude"]
    tags = {}
    for source in (lambda: nws_alert_tags(lat, lon),
                   lambda: open_meteo_tags(lat, lon),
                   lambda: moon_tags(dt.date.today())):
        try:
            for tag, reason in source().items():
                tags.setdefault(tag, reason)
        except Exception as exc:
            log.warning("weather source failed: %s", exc)
    for tag in weather_cfg.get("force_tags") or []:  # testing hook
        tags.setdefault(tag, "forced via config")
    return tags


if __name__ == "__main__":
    # Quick sanity check: print this year's full moons and current tags.
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    year = dt.date.today().year
    print(f"Full moons {year}: {[str(d) for d in full_moon_dates(year)]}")
    if len(sys.argv) == 3:
        cfg = {"latitude": float(sys.argv[1]), "longitude": float(sys.argv[2])}
        print(json.dumps(get_active_tags(cfg), indent=2))
