#!/usr/bin/env python3
"""Per-camera capture availability: how many frames a day should have held,
how many it actually holds, and where the gaps fall.

Deliberately reconstructed from what is on disk rather than from a running
counter. That way history predating any of this still reports, and a capture
process that crashed or was restarted cannot quietly zero the numbers. The
miss log (common.record_capture_miss) adds *why* frames are missing; the
frames themselves establish *that* they are.
"""

import datetime as dt
import logging

from common import (camera_interval_seconds, probe_frame_count,
                    read_capture_misses, snapshots_dir, videos_dir)

log = logging.getLogger("availability")

# Night folders bucket noon-to-noon, so one night lands in one folder.
NIGHT_DAY_SHIFT = dt.timedelta(hours=12)
# Spacing wider than this many intervals counts as a gap rather than jitter.
GAP_FACTOR = 1.5


def expected_ticks(cfg, cam, date, tz=None, now=None, as_bounds=False):
    """How many frames this camera was scheduled to capture for `date`.

    With as_bounds, returns (count, first_sec, last_sec) instead -- the
    seconds-of-day of the first and last scheduled tick, which find_gaps needs
    to see an outage that runs to the end of the day.

    Each candidate tick is gated through capture.within_window, so a daylight-
    or night-limited camera is measured against its own schedule instead of the
    wall clock -- otherwise a night camera reads as permanently 50% down.

    The denominator is the camera's *base* interval. Burst tags shorten the
    interval during a storm, which only ever adds frames, so a stormy day can
    land slightly over 100%; callers clamp it. Sizing the denominator by burst
    would mean reconstructing every burst span, and would get *less* accurate
    exactly when the conditions log has holes in it.
    """
    # Imported here, not at module scope: capture.py owns the window logic and
    # imports common, so a top-level import would be circular.
    import capture

    interval = camera_interval_seconds(cfg, cam)
    if interval <= 0:
        return 0
    daylight = capture.DaylightWindow(cfg, tz, cam)
    start = dt.datetime.combine(date, dt.time.min)
    end = start + dt.timedelta(days=1)
    if daylight.night:
        start += NIGHT_DAY_SHIFT
        end += NIGHT_DAY_SHIFT
    if now is not None and end > now:
        end = now          # today is only partly over
    if end <= start:
        return 0

    ticks = []
    step = dt.timedelta(seconds=interval)
    tick = start
    while tick < end:
        if capture.within_window(tick, cfg["capture"], daylight):
            ticks.append(tick)
        tick += step
    if not as_bounds:
        return len(ticks)
    if not ticks:
        return len(ticks), None, None
    # Seconds-of-day, to line up with the HHMMSS frame names. Skipped for night
    # cameras: their folder spans noon to noon, so a scheduled window crosses
    # midnight and these bounds would be nonsense.
    if daylight.night:
        return len(ticks), None, None
    first = ticks[0].hour * 3600 + ticks[0].minute * 60 + ticks[0].second
    last = ticks[-1].hour * 3600 + ticks[-1].minute * 60 + ticks[-1].second
    return len(ticks), first, last


def frame_seconds(day_dir):
    """Seconds-of-day for every frame in a day folder, from its HHMMSS name."""
    out = []
    for path in day_dir.glob("*.jpg"):
        stem = path.stem
        if len(stem) != 6 or not stem.isdigit():
            continue
        out.append(int(stem[0:2]) * 3600 + int(stem[2:4]) * 60 + int(stem[4:6]))
    out.sort()
    return out


def find_gaps(times, interval, start_sec=None, end_sec=None):
    """Stretches with no frames, as {start, end, missed} in seconds-of-day.

    Derived from the frames themselves, so it reconstructs gaps from before any
    miss logging existed -- including the ones a full disk produced.

    start_sec/end_sec bound the scheduled window. They matter more than they
    look: without them only gaps *between* two frames are visible, so a camera
    that dies and never comes back -- the most common real failure, and exactly
    what a full disk causes -- reports no gap at all, because there is no later
    frame to measure against. Same for a day that starts late.
    """
    if interval <= 0:
        return []
    threshold = interval * GAP_FACTOR
    gaps = []

    def add(start, end):
        gaps.append({"start": start, "end": end,
                     "missed": max(1, int(round((end - start) / interval)))})

    if not times:
        if start_sec is not None and end_sec is not None and end_sec > start_sec:
            add(start_sec, end_sec)
        return gaps

    if start_sec is not None and times[0] - start_sec > threshold:
        add(start_sec, times[0])
    for earlier, later in zip(times, times[1:]):
        span = later - earlier
        if span > threshold:
            gaps.append({"start": earlier + interval, "end": later,
                         "missed": max(1, int(round(span / interval)) - 1)})
    if end_sec is not None and end_sec - times[-1] > threshold:
        add(times[-1] + interval, end_sec)
    return gaps


def hourly_misses(gaps, interval):
    """Missing frames bucketed by the hour they should have landed in."""
    hours = [0] * 24
    if interval <= 0:
        return hours
    for gap in gaps:
        tick = gap["start"]
        while tick < gap["end"]:
            hour = int(tick // 3600)
            if 0 <= hour < 24:
                hours[hour] += 1
            tick += interval
    return hours


def day_report(cfg, date, tz=None, now=None):
    """Availability for every camera on one local date."""
    misses = read_capture_misses(cfg, date)
    cameras = []
    for cam in cfg.get("cameras") or []:
        name = cam["name"]
        interval = camera_interval_seconds(cfg, cam)
        expected, first_sec, last_sec = expected_ticks(
            cfg, cam, date, tz, now, as_bounds=True)
        day_dir = snapshots_dir(cfg) / name / date.isoformat()
        video = videos_dir(cfg) / name / "daily" / (date.isoformat() + ".mp4")

        gaps, hours, source = [], [0] * 24, "frames"
        if day_dir.is_dir():
            times = frame_seconds(day_dir)
            captured = len(times)
            gaps = find_gaps(times, interval, first_sec, last_sec)
            hours = hourly_misses(gaps, interval)
        elif video.exists():
            # Frames already pruned. The built video is the next best witness,
            # but it undercounts whenever event-burst frames were left out of
            # it, so it is reported as a weaker source rather than as fact.
            captured = probe_frame_count(video)
            source = "video" if captured is not None else "unknown"
        else:
            captured = None
            source = "unknown"

        cam_misses = [m for m in misses if m.get("camera") == name]
        reasons = {}
        for miss in cam_misses:
            reason = miss.get("reason") or "error"
            reasons[reason] = reasons.get(reason, 0) + 1

        pct = None
        if captured is not None and expected > 0:
            pct = round(min(100.0, 100.0 * captured / expected), 1)

        cameras.append({
            "camera": name,
            "interval_seconds": interval,
            "expected": expected,
            "captured": captured,
            "missed": None if captured is None else max(0, expected - captured),
            "availability_pct": pct,
            "source": source,
            "gaps": gaps,
            "longest_gap_seconds": max((g["end"] - g["start"] for g in gaps), default=0),
            "hourly_missed": hours,
            "reasons": reasons,
        })
    return {"date": date.isoformat(), "cameras": cameras}


def report(cfg, days=14, tz=None, today=None):
    """Rolling availability, newest day first."""
    from common import local_today
    today = today or local_today(tz)
    now = dt.datetime.now()
    out = []
    for back in range(days):
        date = today - dt.timedelta(days=back)
        out.append(day_report(cfg, date, tz, now if date == today else None))
    return {"days": out, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S")}
