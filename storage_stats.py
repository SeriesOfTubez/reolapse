"""Compute per-camera and system-wide storage usage and growth-rate estimates.

Runs automatically after each nightly daily build (see build_timelapse.py)
and writes data/storage_stats.json, which the web UI's Storage tab reads.
Can also be run standalone: `python storage_stats.py [--config PATH]`.

Everything here is a plain disk scan — no assumptions about image content,
so it works the same whether a camera is 2MP or 12MP.
"""

import argparse
import datetime as dt
import json
import shutil

from common import load_config, snapshots_dir, videos_dir, yearly_frames_dir

GB = 1024 ** 3


def _dir_bytes_and_count(path, pattern="*"):
    if not path.is_dir():
        return 0, 0
    total = count = 0
    for f in path.rglob(pattern):
        if f.is_file():
            total += f.stat().st_size
            count += 1
    return total, count


def _frames_per_day(capture_cfg):
    """How many snapshots/day a camera produces, honoring a start/end window."""
    interval = capture_cfg.get("interval_seconds", 60)
    start, end = capture_cfg.get("start_time"), capture_cfg.get("end_time")
    if start and end:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        window_seconds = max(0, (eh * 3600 + em * 60) - (sh * 3600 + sm * 60))
    else:
        window_seconds = 86400
    return window_seconds / interval if interval else 0


def compute_stats(cfg) -> dict:
    root = cfg["storage"]["root"]
    capture_cfg = cfg["capture"]
    frames_per_day = _frames_per_day(capture_cfg)
    archive_frames_per_day = cfg.get("yearly", {}).get("archive_frames_per_day", 24)

    cameras = {}
    sys_snap_bytes = sys_snap_per_day = 0
    sys_daily_bytes = sys_daily_per_day = 0
    sys_yearly_bytes = sys_yearly_per_day = 0
    sys_events_bytes = 0

    for cam in cfg["cameras"]:
        name = cam["name"]

        snap_bytes, snap_count = _dir_bytes_and_count(snapshots_dir(cfg) / name, "*.jpg")
        avg_frame_bytes = (snap_bytes / snap_count) if snap_count else 0
        snap_per_day = avg_frame_bytes * frames_per_day

        daily_dir = videos_dir(cfg) / name / "daily"
        daily_bytes, daily_count = _dir_bytes_and_count(daily_dir, "*.mp4")
        daily_avg = (daily_bytes / daily_count) if daily_count else 0

        yearly_bytes, _ = _dir_bytes_and_count(yearly_frames_dir(cfg) / name, "*.jpg")
        yearly_per_day = avg_frame_bytes * archive_frames_per_day

        events_bytes, events_count = _dir_bytes_and_count(
            videos_dir(cfg) / name / "events", "*.mp4")

        cameras[name] = {
            "avg_snapshot_kb": round(avg_frame_bytes / 1024, 1),
            "snapshots_gb": round(snap_bytes / GB, 2),
            "snapshots_gb_per_day": round(snap_per_day / GB, 3),
            "daily_video_gb_total": round(daily_bytes / GB, 2),
            "daily_video_count": daily_count,
            "daily_video_avg_mb": round(daily_avg / 1024 / 1024, 1),
            "yearly_frames_gb": round(yearly_bytes / GB, 3),
            "yearly_gb_per_day": round(yearly_per_day / GB, 4),
            "events_gb_total": round(events_bytes / GB, 2),
            "events_count": events_count,
        }

        sys_snap_bytes += snap_bytes
        sys_snap_per_day += snap_per_day
        sys_daily_bytes += daily_bytes
        sys_daily_per_day += daily_avg  # one daily video per camera per day
        sys_yearly_bytes += yearly_bytes
        sys_yearly_per_day += yearly_per_day
        sys_events_bytes += events_bytes

    total_bytes, free_bytes, disk_used_pct = _disk_usage(root)

    system = {
        "snapshots_gb": round(sys_snap_bytes / GB, 2),
        "snapshots_gb_per_day": round(sys_snap_per_day / GB, 3),
        "daily_video_gb_total": round(sys_daily_bytes / GB, 2),
        "daily_video_gb_per_day": round(sys_daily_per_day / GB, 3),
        "yearly_frames_gb": round(sys_yearly_bytes / GB, 3),
        "yearly_gb_per_day": round(sys_yearly_per_day / GB, 4),
        "events_gb_total": round(sys_events_bytes / GB, 2),
        "grand_total_gb": round(
            (sys_snap_bytes + sys_daily_bytes + sys_yearly_bytes + sys_events_bytes) / GB, 2),
        "projected_daily_video_30d_gb": round(sys_daily_per_day * 30 / GB, 1),
        "projected_daily_video_365d_gb": round(sys_daily_per_day * 365 / GB, 1),
        "disk_total_gb": round(total_bytes / GB, 1),
        "disk_free_gb": round(free_bytes / GB, 1),
        "disk_used_pct": disk_used_pct,
    }

    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cameras": cameras,
        "system": system,
    }


def _disk_usage(path):
    path = path if path.exists() else path.parent
    usage = shutil.disk_usage(path)
    used_pct = round(100 * usage.used / usage.total) if usage.total else 0
    return usage.total, usage.free, used_pct


def write_stats(cfg, out_path=None):
    stats = compute_stats(cfg)
    out_path = out_path or (cfg["storage"]["root"] / "storage_stats.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    stats = write_stats(load_config(args.config))
    print(json.dumps(stats, indent=2))
