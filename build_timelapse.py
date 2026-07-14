#!/usr/bin/env python3
"""Build timelapse videos from captured snapshots.

Daily video for yesterday (also archives each day's yearly frame):
    python build_timelapse.py daily

Specific date / camera:
    python build_timelapse.py daily --date 2026-07-04 --camera front-yard

Yearly video (rebuild any time; uses frames archived by the daily build):
    python build_timelapse.py yearly --year 2026
"""

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import (load_config, local_today, snapshots_dir, tzinfo_for,
                    videos_dir, yearly_frames_dir)

log = logging.getLogger("timelapse")

MIN_FRAMES = 2


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        sys.exit(
            "ffmpeg not found on PATH. Install it first:\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  Debian:   sudo apt install ffmpeg"
        )


X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow")


def build_video(frames, out_path: Path, *, fps, crf, deflicker_size, max_height=0,
                metadata=None, preset="medium"):
    """Encode an ordered list of JPEGs into an mp4.

    Frames are hardlinked (copy fallback) into a temp dir as a numbered
    sequence — ffmpeg's image2 demuxer is the most reliable input method on
    Windows, where glob patterns aren't supported. The temp dir lives next to
    the output, NOT in the system temp: /tmp is often a small tmpfs that a
    day's worth of frames overflows, and staying on the snapshots' filesystem
    lets the hardlinks succeed so nothing is copied at all.

    `metadata` (e.g. {"season": "summer"}) is written as MP4 metadata tags —
    readable with `ffprobe` or exiftool, same spirit as the JPEG comment tags
    embedded on frames.

    `preset` trades libx264 encode speed against compression efficiency —
    slower presets produce a smaller file at the same crf, at the cost of
    more CPU time. `medium` is ffmpeg's own default and this project's
    long-standing behavior; falls back to it if given anything not in
    X264_PRESETS rather than passing a bad value through to ffmpeg.
    """
    if preset not in X264_PRESETS:
        log.warning("unknown preset %r, falling back to medium", preset)
        preset = "medium"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="timelapse_", dir=out_path.parent) as tmp:
        tmp = Path(tmp)
        for i, src in enumerate(frames):
            dst = tmp / f"{i:06d}.jpg"
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        filters = []
        if deflicker_size and deflicker_size > 1:
            filters.append(f"deflicker=mode=pm:size={deflicker_size}")
        if max_height:
            filters.append(f"scale=-2:min(ih\\,{max_height})")
        filters.append("format=yuv420p")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-framerate", str(fps),
            "-i", str(tmp / "%06d.jpg"),
            "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        ]
        for key, value in (metadata or {}).items():
            cmd += ["-metadata", f"{key}={value}"]
        # use_metadata_tags: the mov/mp4 muxer otherwise only writes a fixed
        # whitelist of "known" keys (comment, artist, ...) and silently drops
        # anything else, including custom keys like "season".
        movflags = "+faststart+use_metadata_tags" if metadata else "+faststart"
        cmd += ["-movflags", movflags, str(out_path)]
        subprocess.run(cmd, check=True)
    log.info("wrote %s (%d frames, %.1f MB)", out_path, len(frames),
             out_path.stat().st_size / 1e6)


def select_evenly(frames, n):
    """N frames evenly spaced across the list (chunk midpoints)."""
    if n <= 0 or n >= len(frames):
        return list(frames)
    return [frames[int((k + 0.5) * len(frames) / n)] for k in range(n)]


def archive_yearly_frames(cfg, cam_name, date, frames):
    """Copy an hourly-ish subset of the day's frames into the permanent
    yearly archive, named MM-DD_HHMMSS.jpg so the yearly build can select
    by time of day. Re-running a build replaces that day's archive."""
    per_day = cfg["yearly"].get("archive_frames_per_day", 24)
    picks = select_evenly(frames, per_day)
    out_dir = yearly_frames_dir(cfg) / cam_name / str(date.year)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = date.strftime("%m-%d")
    for old in out_dir.glob(f"{day}*.jpg"):
        old.unlink()
    for src in picks:
        shutil.copy2(src, out_dir / f"{day}_{src.stem}.jpg")
    log.info("%s: archived %d yearly frame(s) for %s", cam_name, len(picks), date)


def parse_window(spec):
    """'HH:MM-HH:MM' -> (start_secs, end_secs), or None for empty/missing."""
    if not spec:
        return None
    start, end = spec.split("-")

    def secs(t):
        hour, minute = t.strip().split(":")
        return int(hour) * 3600 + int(minute) * 60

    return secs(start), secs(end)


def select_video_frames(archived, yearly_cfg):
    """Pick the frames for the yearly video from a year's archive:
    group per day, filter to the daylight window, take N evenly spaced."""
    window = parse_window(yearly_cfg.get("video_window"))
    per_day = yearly_cfg.get("video_frames_per_day", 10)

    by_day = {}
    for f in archived:
        by_day.setdefault(f.stem[:5], []).append(f)  # key: "MM-DD"

    picked = []
    for day in sorted(by_day):
        frames = sorted(by_day[day])
        if window:
            in_window = []
            for f in frames:
                parts = f.stem.split("_", 1)
                if len(parts) == 2 and len(parts[1]) == 6 and parts[1].isdigit():
                    if window[0] <= frame_seconds_of(parts[1]) <= window[1]:
                        in_window.append(f)
                else:
                    in_window.append(f)  # legacy MM-DD.jpg: keep
            frames = in_window or frames  # never drop a whole day
        picked.extend(select_evenly(frames, per_day))
    return picked


def frame_seconds_of(hhmmss: str) -> int:
    return int(hhmmss[0:2]) * 3600 + int(hhmmss[2:4]) * 60 + int(hhmmss[4:6])


def day_frames(cfg, cam_name, date):
    day_dir = snapshots_dir(cfg) / cam_name / date.isoformat()
    if not day_dir.is_dir():
        return []
    return sorted(day_dir.glob("*.jpg"))


def season_metadata(cfg, date):
    """{"season": "summer"} if events.season_enabled, else None. Best-effort —
    a lookup failure never blocks the video build it's attached to."""
    if not (cfg.get("events") or {}).get("season_enabled"):
        return None
    try:
        import events as events_mod
        ecfg = cfg["events"]
        lat = None
        try:
            lat, _ = events_mod.resolve_location(ecfg)
        except Exception:
            pass  # season_for_date defaults to Northern Hemisphere without one
        ephem_dir = cfg["storage"]["root"] / "ephemeris"
        return {"season": events_mod.season_for_date(date, ephem_dir, lat)}
    except Exception:
        log.exception("season metadata lookup failed")
        return None


def cmd_daily(cfg, args):
    start = time.time()
    tz = build_timezone(cfg)
    date = resolve_date(args.date, tz)
    season = season_metadata(cfg, date)
    for cam in selected_cameras(cfg, args.camera):
        frames = day_frames(cfg, cam["name"], date)
        if len(frames) < MIN_FRAMES:
            log.warning("%s: only %d frame(s) for %s, skipping", cam["name"], len(frames), date)
            continue
        d = cfg["daily_video"]
        out = videos_dir(cfg) / cam["name"] / "daily" / f"{date.isoformat()}.mp4"
        build_video(
            frames, out,
            fps=d["fps"], crf=d["crf"],
            deflicker_size=d.get("deflicker_size", 0),
            max_height=d.get("max_height", 0),
            preset=d.get("preset", "medium"),
            metadata=season,
        )
        archive_yearly_frames(cfg, cam["name"], date, frames)

    try:
        build_event_videos(cfg, date, args.camera)
    except Exception:
        log.exception("event video build failed (daily videos are unaffected)")

    try:
        for cam in selected_cameras(cfg, args.camera):
            prune_daily_videos(cfg, cam["name"])
        prune_event_videos(cfg)
    except Exception:
        log.exception("video retention pruning failed (build is unaffected)")

    elapsed = time.time() - start
    log.info("daily build finished in %.1f min", elapsed / 60)
    try:
        record_build_time(cfg, date, elapsed)
    except Exception:
        log.exception("build time recording failed")

    try:
        import storage_stats
        storage_stats.write_stats(cfg)
    except Exception:
        log.exception("storage stats update failed (daily videos are unaffected)")


def record_build_time(cfg, date, seconds, keep_last=60):
    """Append this build's duration to a small rolling history file, used to
    compute the "average build time" shown in the Storage tab."""
    path = cfg["storage"]["root"] / "build_times.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"date": date.isoformat(),
             "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "seconds": round(seconds, 1)}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) > keep_last:
        path.write_text("\n".join(lines[-keep_last:]) + "\n", encoding="utf-8")


def prune_daily_videos(cfg, cam_name):
    """Delete a camera's daily videos older than daily_video.retention_days.

    Unlike snapshots, a pruned daily video is not re-buildable — by the time
    it's old enough to prune, its source frames are long gone (snapshot
    retention is measured in days, this in days-to-years).
    """
    days = cfg["daily_video"].get("retention_days", 0)
    if not days:
        return
    cutoff = dt.date.today() - dt.timedelta(days=days)
    daily_dir = videos_dir(cfg) / cam_name / "daily"
    if not daily_dir.is_dir():
        return
    for f in daily_dir.glob("*.mp4"):
        try:
            file_date = dt.date.fromisoformat(f.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            f.unlink()
            log.info("%s: pruned daily video %s (older than %d days)",
                      cam_name, f.name, days)


def prune_yearly_videos(cfg, cam_name):
    """Delete a camera's yearly videos older than yearly.retention_years.

    Only the rendered .mp4 is deleted — the archived frames it was built from
    are never pruned, so a deleted year can be regenerated any time with
    `build_timelapse.py yearly --year YYYY`.
    """
    years = cfg["yearly"].get("retention_years", 0)
    if not years:
        return
    cutoff_year = dt.date.today().year - years
    yearly_dir = videos_dir(cfg) / cam_name / "yearly"
    if not yearly_dir.is_dir():
        return
    for f in yearly_dir.glob("*.mp4"):
        try:
            year = int(f.stem)
        except ValueError:
            continue
        if year < cutoff_year:
            f.unlink()
            log.info("%s: pruned yearly video %s (older than %d years; frames are "
                      "kept forever — rebuild any time with `yearly --year %d`)",
                      cam_name, f.name, years, year)


def prune_event_videos(cfg):
    """Delete event clips older than events_video.retention_days, then drop
    their entries from the events.jsonl index so it stays in sync with disk.
    """
    days = cfg.get("events_video", {}).get("retention_days", 0)
    if days:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        for cam in cfg["cameras"]:
            events_dir = videos_dir(cfg) / cam["name"] / "events"
            if not events_dir.is_dir():
                continue
            for f in events_dir.glob("*.mp4"):
                try:
                    file_date = dt.date.fromisoformat(f.stem[:10])
                except ValueError:
                    continue
                if file_date < cutoff:
                    f.unlink()
                    log.info("%s: pruned event video %s (older than %d days)",
                              cam["name"], f.name, days)

    index_path = cfg["storage"]["root"] / "events.jsonl"
    if not index_path.exists():
        return
    kept = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if (videos_dir(cfg) / entry["video"]).exists():
            kept.append(entry)
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in kept:
            f.write(json.dumps(entry) + "\n")


def tag_spans(cfg, date, tag, gap_minutes=20):
    """Reconstruct when a tag was active on a date from the conditions log.

    The log records tag-set *changes*, so state at midnight is seeded from
    the last entry of the previous day's file.
    """
    cond_dir = cfg["storage"]["root"] / "conditions"

    def entries(day):
        path = cond_dir / f"{day.isoformat()}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    day_start = dt.datetime.combine(date, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)

    active_since = None
    prev = entries(date - dt.timedelta(days=1))
    if prev and tag in prev[-1]["tags"]:
        active_since = day_start

    spans = []
    for entry in entries(date):
        ts = dt.datetime.strptime(entry["ts"], "%Y-%m-%d %H:%M:%S")
        has = tag in entry["tags"]
        if has and active_since is None:
            active_since = ts
        elif not has and active_since is not None:
            spans.append((active_since, ts))
            active_since = None
    if active_since is not None:
        spans.append((active_since, day_end))

    merged = []
    for start, end in spans:
        if merged and (start - merged[-1][1]).total_seconds() <= gap_minutes * 60:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def build_event_videos(cfg, date, camera=None):
    ev = cfg.get("events_video") or {}
    tags = ev.get("tags") or []
    if not tags:
        return
    min_frames = ev.get("min_frames", 30)
    index_path = cfg["storage"]["root"] / "events.jsonl"

    # Drop stale index entries for this date before re-adding (rebuild-safe)
    index = []
    if index_path.exists():
        index = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and json.loads(line).get("date") != date.isoformat()]

    deflicker_by_tag = ev.get("deflicker_by_tag") or {}
    default_deflicker = ev.get("deflicker_size", 0)
    season = season_metadata(cfg, date)

    for cam in selected_cameras(cfg, camera):
        all_frames = day_frames(cfg, cam["name"], date)
        for tag in tags:
            for i, (start, end) in enumerate(tag_spans(cfg, date, tag)):
                frames = [f for f in all_frames
                          if start.strftime("%H%M%S") <= f.stem <= end.strftime("%H%M%S")]
                if len(frames) < min_frames:
                    continue
                suffix = f"-{i + 1}" if i else ""
                out = (videos_dir(cfg) / cam["name"] / "events"
                       / f"{date.isoformat()}_{tag}{suffix}.mp4")
                build_video(
                    frames, out,
                    fps=ev.get("fps", 30), crf=ev.get("crf", 20),
                    # deflicker defaults to off (storm's lightning flashes
                    # would get smoothed away) but is configurable per tag —
                    # e.g. snow has no lightning to protect
                    deflicker_size=deflicker_by_tag.get(tag, default_deflicker),
                    max_height=cfg["daily_video"].get("max_height", 0),
                    preset=ev.get("preset", "medium"),
                    metadata=season,
                )
                index.append({
                    "date": date.isoformat(), "tag": tag, "camera": cam["name"],
                    "video": f"{cam['name']}/events/{out.name}",
                    "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                    "frames": len(frames),
                })

    with open(index_path, "w", encoding="utf-8") as f:
        for entry in index:
            f.write(json.dumps(entry) + "\n")


def cmd_events(cfg, args):
    build_event_videos(cfg, resolve_date(args.date, build_timezone(cfg)), args.camera)


def cmd_yearly(cfg, args):
    year = args.year or local_today(build_timezone(cfg)).year
    # Don't render a yearly video from just a handful of days — a couple of
    # days is a ~2-second clip that's more confusing than useful. Wait until
    # this many distinct days have been archived (0 = render as soon as there
    # are any frames). --force overrides it for an intentional early build.
    min_days = cfg["yearly"].get("min_days_before_render", 30)
    for cam in selected_cameras(cfg, args.camera):
        frame_dir = yearly_frames_dir(cfg) / cam["name"] / str(year)
        archived = sorted(frame_dir.glob("*.jpg")) if frame_dir.is_dir() else []
        days_archived = len({f.stem[:5] for f in archived})  # distinct MM-DD
        if not getattr(args, "force", False) and days_archived < min_days:
            log.info("%s: %d day(s) of yearly frames for %s — holding off until %d "
                     "(yearly.min_days_before_render); pass --force to build now",
                     cam["name"], days_archived, year, min_days)
            continue
        frames = select_video_frames(archived, cfg["yearly"])
        if len(frames) < MIN_FRAMES:
            log.warning("%s: only %d yearly frame(s) for %s, skipping — "
                        "run daily builds first", cam["name"], len(frames), year)
            continue
        y = cfg["yearly"]
        out = videos_dir(cfg) / cam["name"] / "yearly" / f"{year}.mp4"
        build_video(
            frames, out,
            fps=y["fps"], crf=y["crf"],
            deflicker_size=y.get("deflicker_size", 0),
            max_height=cfg["daily_video"].get("max_height", 0),
            preset=y.get("preset", "medium"),
        )
        try:
            prune_yearly_videos(cfg, cam["name"])
        except Exception:
            log.exception("%s: yearly video retention pruning failed", cam["name"])


def build_timezone(cfg):
    """Configured-or-auto tzinfo (or None for host) so the build's notion of
    'today'/'yesterday' matches the timezone capture used to bucket frames."""
    import events
    return tzinfo_for(events.resolve_timezone(cfg))


def resolve_date(value, tz=None) -> dt.date:
    if value in (None, "yesterday"):
        return local_today(tz) - dt.timedelta(days=1)
    if value == "today":
        return local_today(tz)
    return dt.date.fromisoformat(value)


def selected_cameras(cfg, name):
    cams = cfg["cameras"]
    if name:
        cams = [c for c in cams if c["name"] == name]
        if not cams:
            sys.exit(f"No camera named {name!r} in config")
    return cams


def main():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", default=None, help="path to config.yaml")

    parser = argparse.ArgumentParser(description=__doc__, parents=[shared],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_daily = sub.add_parser("daily", parents=[shared],
                             help="build a daily video and archive yearly frames")
    p_daily.add_argument("--date", default="yesterday",
                         help="YYYY-MM-DD, 'today', or 'yesterday' (default)")
    p_daily.add_argument("--camera", default=None, help="only this camera")

    p_yearly = sub.add_parser("yearly", parents=[shared],
                              help="build the yearly video from archived frames")
    p_yearly.add_argument("--year", type=int, default=None, help="default: current year")
    p_yearly.add_argument("--camera", default=None, help="only this camera")
    p_yearly.add_argument("--force", action="store_true",
                          help="build even if fewer than yearly.min_days_before_render days exist")

    p_events = sub.add_parser("events", parents=[shared],
                              help="(re)build event videos from the conditions log")
    p_events.add_argument("--date", default="yesterday",
                          help="YYYY-MM-DD, 'today', or 'yesterday' (default)")
    p_events.add_argument("--camera", default=None, help="only this camera")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    require_ffmpeg()
    cfg = load_config(args.config)
    if args.command == "daily":
        cmd_daily(cfg, args)
    elif args.command == "events":
        cmd_events(cfg, args)
    else:
        cmd_yearly(cfg, args)


if __name__ == "__main__":
    main()
