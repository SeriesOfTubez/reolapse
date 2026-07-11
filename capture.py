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
import time
import uuid

import requests
import urllib3

import weather
from common import load_config, snapshots_dir, videos_dir

log = logging.getLogger("capture")

JPEG_MAGIC = b"\xff\xd8"


class Conditions:
    """Tracks active weather/astronomy tags, throttled to poll_minutes.

    Tag changes are appended to data/conditions/<date>.jsonl — the metadata
    log that event videos and future search/filter features are built from.
    """

    def __init__(self, cfg):
        wcfg = cfg.get("weather") or {}
        self.wcfg = wcfg
        self.enabled = bool(wcfg.get("enabled"))
        self.log_dir = cfg["storage"]["root"] / "conditions"
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
        self.tags = weather.get_active_tags(self.wcfg)
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
            return self.wcfg.get("burst_interval_seconds", 10)
        return base_seconds


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


def within_window(now, capture_cfg) -> bool:
    start = parse_hhmm(capture_cfg.get("start_time"))
    end = parse_hhmm(capture_cfg.get("end_time"))
    if start and now.time() < start:
        return False
    if end and now.time() > end:
        return False
    return True


def prune_old_snapshots(cfg):
    """Delete day folders past retention, but only once their daily video exists."""
    keep_days = cfg["storage"].get("keep_snapshots_days", 0)
    if not keep_days:
        return
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)

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


def run_once(cfg, conditions=None):
    now = dt.datetime.now()
    capture_cfg = cfg["capture"]
    if not within_window(now, capture_cfg):
        log.debug("outside capture window, skipping")
        return

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
                path, size = take_snapshot(cam, capture_cfg, out_root, now, quarantine, tags)
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


def loop(cfg):
    base_interval = cfg["capture"]["interval_seconds"]
    conditions = Conditions(cfg)
    last_prune_day = None
    while True:
        conditions.refresh()
        # Burst tags (storm/snow) shorten the interval; pick a burst interval
        # that divides the base one so burst frames stay clock-aligned too.
        interval = conditions.interval(base_interval)
        now = time.time()
        time.sleep(interval - (now % interval))
        run_once(cfg, conditions)

        today = dt.date.today()
        if today != last_prune_day:
            prune_old_snapshots(cfg)
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
    cfg = load_config(args.config)

    if args.loop:
        loop(cfg)
    else:
        conditions = Conditions(cfg)
        conditions.refresh()
        run_once(cfg, conditions)
        prune_old_snapshots(cfg)


if __name__ == "__main__":
    main()
