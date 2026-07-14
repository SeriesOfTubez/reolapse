#!/usr/bin/env python3
"""Grab snapshots from Reolink cameras (or an NVR) for timelapse building.

Single shot (run from a scheduler):
    python capture.py

Continuous, aligned to the configured interval (run as a service / at startup):
    python capture.py --loop
"""

import argparse
import datetime as dt
import json
import logging
import shutil
import subprocess
import sys
import time
import uuid

import requests
import urllib3

import events
from common import (APP_ROOT, APP_VERSION, load_config, local_now, local_today,
                    snapshots_dir, tzinfo_for, videos_dir)

# A "night" spans midnight, so night frames are bucketed by a noon-to-noon
# logical day (shift the timestamp back 12h). That makes one evening + the
# following morning land in a single folder — one continuous video — and sort
# in chronological order by filename.
NIGHT_DAY_SHIFT = dt.timedelta(hours=12)
# After a night's capture window closes in the morning, wait this long before
# kicking off its build, so the last frames are settled.
NIGHT_BUILD_DELAY = dt.timedelta(minutes=5)

log = logging.getLogger("capture")

JPEG_MAGIC = b"\xff\xd8"

# Floor on how often we poll a device for a snapshot. Sub-10s polling risks
# loading the camera/NVR (and hammers battery cameras behind a hub); 10s is
# also the storm-burst interval, so it's a sane, tested minimum. Enforced here
# and rejected by the web config validator.
MIN_INTERVAL_SECONDS = 10


class Conditions:
    """Tracks active weather/astronomy tags, throttled to poll_minutes.

    Tag changes are appended to data/conditions/<date>.jsonl — the metadata
    log that event videos and future search/filter features are built from.
    """

    def __init__(self, cfg):
        wcfg = cfg.get("events") or {}
        self.wcfg = wcfg
        self.enabled = (bool(wcfg.get("weather_enabled")) or bool(wcfg.get("lunar_enabled"))
                        or bool(wcfg.get("season_enabled")))
        self.log_dir = cfg["storage"]["root"] / "conditions"
        self.ephem_dir = cfg["storage"]["root"] / "ephemeris"
        self.tags = {}
        self._known = None
        self._last_poll = None

    def refresh(self):
        if not self.enabled:
            return
        now = dt.datetime.now()
        poll_secs = self.wcfg.get("poll_minutes", 5) * 60
        if self._last_poll and (now - self._last_poll).total_seconds() < poll_secs:
            return
        self._last_poll = now
        self.tags = events.get_active_tags(self.wcfg, self.ephem_dir)
        if set(self.tags) != self._known:
            self._known = set(self.tags)
            log.info("conditions now: %s", sorted(self.tags) or "clear")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            entry = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                     "tags": sorted(self.tags), "detail": self.tags}
            with open(self.log_dir / f"{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def interval(self, base_seconds):
        burst_tags = set(self.wcfg.get("burst_tags") or [])
        if burst_tags & set(self.tags):
            return max(MIN_INTERVAL_SECONDS, self.wcfg.get("burst_interval_seconds", 10))
        return base_seconds


class DaylightWindow:
    """Computes today's sunrise/sunset capture window (with a buffer) once
    per day and caches it — capture ticks call this far more often than the
    sun rises.

    Uses the same location config as weather/lunar tagging (events.zip or
    latitude/longitude), independent of whether either of those is enabled.
    Fails open (captures all day) if location or the sunrise/sunset lookup
    is unavailable, same policy as the rest of capture.py's checks.
    """

    def __init__(self, cfg, tz=None):
        self.cfg = cfg["capture"].get("daylight_window") or {}
        self.enabled = bool(self.cfg.get("enabled"))
        # "day" captures inside the sunrise/sunset window; "night" captures its
        # complement (the dark hours), which spans midnight.
        self.mode = (self.cfg.get("mode") or "day").strip().lower()
        self.events_cfg = cfg.get("events") or {}
        self.ephem_dir = cfg["storage"]["root"] / "ephemeris"
        self.tz = tz
        self._cached_date = None
        self._window = (None, None)

    def window_for(self, today):
        if today != self._cached_date:
            self._cached_date = today
            self._window = self._compute(today)
        return self._window

    def _compute(self, today):
        buffer_min = self.cfg.get("buffer_minutes", 0)
        try:
            lat, lon = events.resolve_location(self.events_cfg)
            sunrise, sunset = events.sunrise_sunset(today, lat, lon, self.ephem_dir, self.tz)
        except Exception as exc:
            log.warning("daylight window unavailable, capturing all day: %s", exc)
            return None, None
        if sunrise:
            sunrise = (dt.datetime.combine(today, sunrise)
                       - dt.timedelta(minutes=buffer_min)).time()
        if sunset:
            sunset = (dt.datetime.combine(today, sunset)
                      + dt.timedelta(minutes=buffer_min)).time()
        log.info("daylight window for %s: %s - %s", today,
                 sunrise.strftime("%H:%M") if sunrise else "n/a",
                 sunset.strftime("%H:%M") if sunset else "n/a")
        return sunrise, sunset


def jpeg_with_comment(data: bytes, payload: dict) -> bytes:
    """Insert a JPEG COM segment (readable by exiftool as 'Comment') after SOI."""
    com = json.dumps(payload, separators=(",", ":")).encode()
    segment = b"\xff\xfe" + (len(com) + 2).to_bytes(2, "big") + com
    return data[:2] + segment + data[2:]


def api_url(cam, host=None) -> str:
    scheme = "https" if cam.get("https") else "http"
    return f"{scheme}://{host or cam['host']}/cgi-bin/api.cgi"


def ptz_at_home(cam, capture_cfg):
    """For cameras with a ptz_home config: is the camera at its home position?

    Returns True when at home (or when the check can't run — fail open so a
    flaky position API never silences capture). The position query goes to the
    camera itself when ptz_home.host is set, because NVRs proxy only the pan
    axis of GetPtzCurPos.
    """
    home = cam.get("ptz_home")
    if not home:
        return True
    try:
        resp = requests.post(
            api_url(cam, host=home.get("host")),
            params={"cmd": "GetPtzCurPos",
                    "user": cam["username"], "password": cam["password"]},
            json=[{"cmd": "GetPtzCurPos", "action": 0,
                   "param": {"PtzCurPos": {"channel": home.get("channel", 0)}}}],
            timeout=capture_cfg.get("timeout_seconds", 15),
            verify=cam.get("verify_ssl", True),
        )
        pos = resp.json()[0]["value"]["PtzCurPos"]
    except Exception as exc:
        log.warning("%s: PTZ position check failed (%s); keeping frame", cam["name"], exc)
        return True
    tol = home.get("tolerance", 10)
    pan, tilt = pos.get("Ppos"), pos.get("Tpos")
    if pan is not None and abs(pan - home["pan"]) > tol:
        return False
    if tilt is not None and "tilt" in home and abs(tilt - home["tilt"]) > tol:
        return False
    return True


def take_snapshot(cam, capture_cfg, out_root, now, quarantine=False, tags=None):
    params = {
        "cmd": "Snap",
        "channel": cam.get("channel", 0),
        "rs": uuid.uuid4().hex[:16],
        "user": cam["username"],
        "password": cam["password"],
    }
    resp = requests.get(
        api_url(cam),
        params=params,
        timeout=capture_cfg.get("timeout_seconds", 15),
        verify=cam.get("verify_ssl", True),
    )
    resp.raise_for_status()
    data = resp.content
    if not data.startswith(JPEG_MAGIC):
        # Reolink returns a JSON error body with HTTP 200 on bad auth etc.
        raise RuntimeError(f"response is not a JPEG: {data[:200]!r}")
    if tags:
        data = jpeg_with_comment(data, {"tags": tags})

    day_dir = out_root / cam["name"] / now.strftime("%Y-%m-%d")
    if quarantine:
        # Off-position frames are kept but excluded from video builds
        # (which only read *.jpg directly inside the day folder).
        day_dir = day_dir / "offposition"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / (now.strftime("%H%M%S") + ".jpg")
    path.write_bytes(data)
    return path, len(data)


def parse_hhmm(value):
    if not value:
        return None
    hour, minute = value.split(":")
    return dt.time(int(hour), int(minute))


def within_window(now, capture_cfg, daylight=None) -> bool:
    if daylight and daylight.enabled:
        start, end = daylight.window_for(now.date())
        if start is None and end is None:
            return True  # sunrise/sunset unavailable -> fail open (capture)
        in_daylight = not ((start and now.time() < start) or (end and now.time() > end))
        # Night mode captures the complement of the daylight window.
        return (not in_daylight) if daylight.mode == "night" else in_daylight

    start = parse_hhmm(capture_cfg.get("start_time"))
    end = parse_hhmm(capture_cfg.get("end_time"))
    if start and now.time() < start:
        return False
    if end and now.time() > end:
        return False
    return True


def prune_old_snapshots(cfg, tz=None):
    """Delete day folders past retention, but only once their daily video exists."""
    keep_days = cfg["storage"].get("keep_snapshots_days", 0)
    if not keep_days:
        return
    cutoff = local_today(tz) - dt.timedelta(days=keep_days)

    snap_root = snapshots_dir(cfg)
    if not snap_root.exists():
        return
    for cam_dir in snap_root.iterdir():
        if not cam_dir.is_dir():
            continue
        for day_dir in cam_dir.iterdir():
            try:
                day = dt.date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if day >= cutoff:
                continue
            video = videos_dir(cfg) / cam_dir.name / "daily" / f"{day_dir.name}.mp4"
            if not video.exists():
                log.warning(
                    "not pruning %s/%s: daily video was never built", cam_dir.name, day_dir.name
                )
                continue
            shutil.rmtree(day_dir)
            log.info("pruned snapshots %s/%s", cam_dir.name, day_dir.name)


def run_once(cfg, conditions=None, daylight=None, tz=None):
    now = local_now(tz)
    capture_cfg = cfg["capture"]
    if not within_window(now, capture_cfg, daylight):
        log.debug("outside capture window, skipping")
        return

    # In night mode, bucket by the noon-to-noon logical day so a night's
    # evening + following-morning frames share one folder and sort in order.
    night = bool(daylight and daylight.enabled and daylight.mode == "night")
    capture_dt = (now - NIGHT_DAY_SHIFT) if night else now

    tags = sorted(conditions.tags) if conditions else []
    out_root = snapshots_dir(cfg)
    for cam in cfg["cameras"]:
        if not cam.get("verify_ssl", True):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        quarantine = not ptz_at_home(cam, capture_cfg)
        if quarantine:
            log.info("%s: not at home position, quarantining frame", cam["name"])
        for attempt in (1, 2):
            try:
                path, size = take_snapshot(cam, capture_cfg, out_root, capture_dt, quarantine, tags)
                log.info("%s: saved %s (%d KB)", cam["name"], path.name, size // 1024)
                break
            except Exception as exc:
                # requests errors embed the full URL, password included
                msg = str(exc).replace(cam["password"], "***")
                if attempt == 1:
                    log.warning("%s: snapshot failed (%s), retrying", cam["name"], msg)
                    time.sleep(2)
                else:
                    log.error("%s: snapshot failed: %s", cam["name"], msg)


def trigger_night_build(config_path, date):
    """Launch the daily build for a just-completed night as a detached process,
    so a slow ffmpeg run never stalls capture. build_timelapse handles its own
    logging and failures; a bad build must not take capture down."""
    cmd = [sys.executable, str(APP_ROOT / "build_timelapse.py"),
           "daily", "--date", date.isoformat()]
    if config_path:
        cmd += ["--config", str(config_path)]
    log.info("night ended — launching build for %s", date)
    try:
        subprocess.Popen(cmd)
    except Exception:
        log.exception("failed to launch night build for %s", date)


def loop(cfg, config_path=None):
    configured_interval = cfg["capture"]["interval_seconds"]
    base_interval = max(MIN_INTERVAL_SECONDS, configured_interval)
    if configured_interval < MIN_INTERVAL_SECONDS:
        log.warning("capture.interval_seconds=%s is below the %ds minimum — using %ds "
                    "(faster polling risks overloading the camera/NVR)",
                    configured_interval, MIN_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS)
    tz = tzinfo_for(events.resolve_timezone(cfg))
    log.info("capture timezone: %s", tz.key if tz is not None else "host system default")
    conditions = Conditions(cfg)
    daylight = DaylightWindow(cfg, tz)
    night_mode = daylight.enabled and daylight.mode == "night"
    if daylight.enabled and (cfg["capture"].get("start_time") or cfg["capture"].get("end_time")):
        log.warning("capture.daylight_window is enabled; static start_time/end_time are ignored")
    if night_mode:
        log.info("night mode: capturing dark hours; each night's video is built "
                 "~%d min after the window closes at dawn", NIGHT_BUILD_DELAY.seconds // 60)
    last_prune_day = None
    was_in_window = None
    pending_build_date = None
    pending_build_at = None
    while True:
        conditions.refresh()
        # Burst tags (storm/snow) shorten the interval; pick a burst interval
        # that divides the base one so burst frames stay clock-aligned too.
        interval = conditions.interval(base_interval)
        now = time.time()
        time.sleep(interval - (now % interval))
        run_once(cfg, conditions, daylight, tz)

        now_local = local_now(tz)
        if night_mode:
            # When capture crosses from the night window into daylight at dawn,
            # the night just finished — schedule its build a few minutes later.
            in_window = within_window(now_local, cfg["capture"], daylight)
            if was_in_window and not in_window:
                pending_build_date = (now_local - NIGHT_DAY_SHIFT).date()
                pending_build_at = now_local + NIGHT_BUILD_DELAY
                log.info("night window closed; build for %s scheduled ~%s",
                         pending_build_date, pending_build_at.strftime("%H:%M"))
            if pending_build_date and now_local >= pending_build_at:
                trigger_night_build(config_path, pending_build_date)
                pending_build_date = pending_build_at = None
            was_in_window = in_window

        today = local_today(tz)
        if today != last_prune_day:
            prune_old_snapshots(cfg, tz)
            last_prune_day = today


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("ReoLapse capture v%s", APP_VERSION)
    cfg = load_config(args.config)

    if args.loop:
        loop(cfg, args.config)
    else:
        tz = tzinfo_for(events.resolve_timezone(cfg))
        conditions = Conditions(cfg)
        conditions.refresh()
        run_once(cfg, conditions, DaylightWindow(cfg, tz), tz)
        prune_old_snapshots(cfg, tz)


if __name__ == "__main__":
    main()
