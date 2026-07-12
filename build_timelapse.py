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
from pathlib import Path

from common import load_config, snapshots_dir, videos_dir, yearly_frames_dir

log = logging.getLogger("timelapse")

MIN_FRAMES = 2


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        sys.exit(
            "ffmpeg not found on PATH. Install it first:\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  Debian:   sudo apt install ffmpeg"
        )


def build_video(frames, out_path: Path, *, fps, crf, deflicker_size, max_height=0):
    """Encode an ordered list of JPEGs into an mp4.

    Frames are hardlinked (copy fallback) into a temp dir as a numbered
    sequence — ffmpeg's image2 demuxer is the most reliable input method on
    Windows, where glob patterns aren't supported. The temp dir lives next to
    the output, NOT in the system temp: /tmp is often a small tmpfs that a
    day's worth of frames overflows, and staying on the snapshots' filesystem
    lets the hardlinks succeed so nothing is copied at all.
    """
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
            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-movflags", "+faststart",
            str(out_path),
        ]
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


def cmd_daily(cfg, args):
    date = resolve_date(args.date)
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
        )
        archive_yearly_frames(cfg, cam["name"], date, frames)
    try:
        build_event_videos(cfg, date, args.camera)
    except Exception:
        log.exception("event video build failed (daily videos are unaffected)")


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
    build_event_videos(cfg, resolve_date(args.date), args.camera)


def cmd_yearly(cfg, args):
    year = args.year or dt.date.today().year
    for cam in selected_cameras(cfg, args.camera):
        frame_dir = yearly_frames_dir(cfg) / cam["name"] / str(year)
        archived = sorted(frame_dir.glob("*.jpg")) if frame_dir.is_dir() else []
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
        )


def resolve_date(value) -> dt.date:
    if value in (None, "yesterday"):
        return dt.date.today() - dt.timedelta(days=1)
    if value == "today":
        return dt.date.today()
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
