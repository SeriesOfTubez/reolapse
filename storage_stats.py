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


def _days_running(cfg):
    """Days since the earliest snapshot day-folder — used to amortize
    irregular accumulations (event clips) into a rough daily rate."""
    root = snapshots_dir(cfg)
    earliest = None
    if root.is_dir():
        for cam_dir in root.iterdir():
            if not cam_dir.is_dir():
                continue
            for day_dir in cam_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    d = dt.date.fromisoformat(day_dir.name)
                except ValueError:
                    continue
                if earliest is None or d < earliest:
                    earliest = d
    if earliest is None:
        return 1
    return max(1, (dt.date.today() - earliest).days + 1)


def compute_stats(cfg) -> dict:
    root = cfg["storage"]["root"]
    capture_cfg = cfg["capture"]
    frames_per_day = _frames_per_day(capture_cfg)
    archive_frames_per_day = cfg.get("yearly", {}).get("archive_frames_per_day", 24)

    cameras = {}
    sys_snap_bytes = sys_snap_per_day = 0
    sys_daily_bytes = sys_daily_per_day = 0
    sys_yearly_frame_bytes = sys_yearly_per_day = 0
    sys_yearly_video_bytes = 0
    sys_events_bytes = 0

    for cam in cfg["cameras"]:
        name = cam["name"]

        snap_bytes, snap_count = _dir_bytes_and_count(snapshots_dir(cfg) / name, "*.jpg")
        avg_frame_bytes = (snap_bytes / snap_count) if snap_count else 0
        snap_per_day = avg_frame_bytes * frames_per_day

        daily_bytes, daily_count = _dir_bytes_and_count(
            videos_dir(cfg) / name / "daily", "*.mp4")
        daily_avg = (daily_bytes / daily_count) if daily_count else 0

        yearly_frame_bytes, _ = _dir_bytes_and_count(yearly_frames_dir(cfg) / name, "*.jpg")
        yearly_per_day = avg_frame_bytes * archive_frames_per_day

        yearly_video_bytes, yearly_video_count = _dir_bytes_and_count(
            videos_dir(cfg) / name / "yearly", "*.mp4")
        yearly_video_avg = (yearly_video_bytes / yearly_video_count) if yearly_video_count else 0

        events_bytes, events_count = _dir_bytes_and_count(
            videos_dir(cfg) / name / "events", "*.mp4")

        cameras[name] = {
            "avg_snapshot_kb": round(avg_frame_bytes / 1024, 1),
            "snapshots_gb": round(snap_bytes / GB, 2),
            "snapshots_gb_per_day": round(snap_per_day / GB, 3),
            "daily_video_gb_total": round(daily_bytes / GB, 2),
            "daily_video_count": daily_count,
            "daily_video_avg_mb": round(daily_avg / 1024 / 1024, 1),
            "yearly_frames_gb": round(yearly_frame_bytes / GB, 3),
            "yearly_gb_per_day": round(yearly_per_day / GB, 4),
            "yearly_video_gb_total": round(yearly_video_bytes / GB, 3),
            "yearly_video_count": yearly_video_count,
            "yearly_video_avg_mb": round(yearly_video_avg / 1024 / 1024, 1),
            "events_gb_total": round(events_bytes / GB, 2),
            "events_count": events_count,
        }

        sys_snap_bytes += snap_bytes
        sys_snap_per_day += snap_per_day
        sys_daily_bytes += daily_bytes
        sys_daily_per_day += daily_avg  # one daily video per camera per day
        sys_yearly_frame_bytes += yearly_frame_bytes
        sys_yearly_per_day += yearly_per_day
        sys_yearly_video_bytes += yearly_video_bytes
        sys_events_bytes += events_bytes

    total_bytes, free_bytes, disk_used_pct = _disk_usage(root)

    system = {
        "snapshots_gb": round(sys_snap_bytes / GB, 2),
        "snapshots_gb_per_day": round(sys_snap_per_day / GB, 3),
        "daily_video_gb_total": round(sys_daily_bytes / GB, 2),
        "daily_video_gb_per_day": round(sys_daily_per_day / GB, 3),
        "yearly_frames_gb": round(sys_yearly_frame_bytes / GB, 3),
        "yearly_gb_per_day": round(sys_yearly_per_day / GB, 4),
        "yearly_video_gb_total": round(sys_yearly_video_bytes / GB, 3),
        "events_gb_total": round(sys_events_bytes / GB, 2),
        "grand_total_gb": round(
            (sys_snap_bytes + sys_daily_bytes + sys_yearly_frame_bytes
             + sys_yearly_video_bytes + sys_events_bytes) / GB, 2),
        "projected_daily_video_30d_gb": round(sys_daily_per_day * 30 / GB, 1),
        "projected_daily_video_365d_gb": round(sys_daily_per_day * 365 / GB, 1),
        "disk_total_gb": round(total_bytes / GB, 1),
        "disk_free_gb": round(free_bytes / GB, 1),
        "disk_used_pct": disk_used_pct,
    }

    days_running = _days_running(cfg)
    system["retention"] = _retention_summary(cfg)
    system["build_time"] = _build_time_summary(cfg)
    system["forecast"] = _forecast(cfg, system, days_running)

    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cameras": cameras,
        "system": system,
    }


def _retention_summary(cfg):
    """0 means "forever" throughout — surfaced as-is; the UI/README explain it."""
    return {
        "snapshot_days": cfg["storage"].get("keep_snapshots_days", 0),
        "daily_video_days": cfg["daily_video"].get("retention_days", 0),
        "yearly_video_years": cfg.get("yearly", {}).get("retention_years", 0),
        "events_video_days": cfg.get("events_video", {}).get("retention_days", 0),
    }


def _build_time_summary(cfg, sample_limit=60):
    path = cfg["storage"]["root"] / "build_times.jsonl"
    if not path.exists():
        return {"avg_seconds": None, "avg_minutes": None, "last_seconds": None, "samples": 0}
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
               if ln.strip()][-sample_limit:]
    if not entries:
        return {"avg_seconds": None, "avg_minutes": None, "last_seconds": None, "samples": 0}
    seconds = [e["seconds"] for e in entries]
    avg = sum(seconds) / len(seconds)
    return {
        "avg_seconds": round(avg, 1),
        "avg_minutes": round(avg / 60, 1),
        "last_seconds": seconds[-1],
        "samples": len(seconds),
    }


def _human_duration(days: float) -> str:
    if days < 1:
        return f"~{round(days * 24)} hours"
    if days < 60:
        return f"~{round(days)} days"
    if days < 730:
        return f"~{round(days / 30)} months"
    return f"~{round(days / 365, 1)} years"


def _forecast(cfg, system, days_running):
    """Where storage is headed: a steady-state ceiling for tiers with finite
    retention, plus a runway estimate for whatever still grows forever (the
    yearly archive frames always do — that's a deliberate, non-negotiable
    design choice, since they're the only way to ever regenerate a pruned
    yearly video).
    """
    retention = _retention_summary(cfg)
    events_gb_per_day = system["events_gb_total"] / days_running

    ceiling_gb = 0.0     # eventual size of every finite-retention tier
    current_gb = 0.0     # what those same tiers occupy right now
    unbounded_gb_per_day = 0.0  # tiers with no cap — grows forever

    def bound(days_or_years, rate_per_day, current):
        nonlocal ceiling_gb, current_gb, unbounded_gb_per_day
        if days_or_years:
            ceiling_gb += rate_per_day * days_or_years
            current_gb += current
        else:
            unbounded_gb_per_day += rate_per_day

    bound(retention["snapshot_days"], system["snapshots_gb_per_day"], system["snapshots_gb"])
    bound(retention["daily_video_days"], system["daily_video_gb_per_day"], system["daily_video_gb_total"])
    bound(retention["events_video_days"], events_gb_per_day, system["events_gb_total"])
    # Yearly archive frames: always unbounded, no config can cap this.
    unbounded_gb_per_day += system["yearly_gb_per_day"]
    # Yearly rendered videos: tiny (one file/camera/year) and capped separately
    # in years, not days — fold its (small) eventual size in directly.
    if retention["yearly_video_years"]:
        ceiling_gb += system["yearly_video_gb_total"]  # already near steady state or shrinking
        current_gb += system["yearly_video_gb_total"]
    else:
        # Grows by ~one small file/camera/year — negligible, not worth its
        # own rate term, but it IS unbounded, so note it qualitatively only.
        pass

    growth_needed_gb = max(0.0, ceiling_gb - current_gb)
    remaining_free_gb = system["disk_free_gb"] - growth_needed_gb

    result = {
        "bounded_ceiling_gb": round(ceiling_gb, 1),
        "bounded_current_gb": round(current_gb, 1),
        "growth_needed_gb": round(growth_needed_gb, 1),
        "unbounded_gb_per_day": round(unbounded_gb_per_day, 3),
        "unbounded_gb_per_year": round(unbounded_gb_per_day * 365, 1),
    }

    if remaining_free_gb <= 0:
        result["verdict"] = "shortage"
        result["shortage_gb"] = round(-remaining_free_gb, 1)
        result["runway_days"] = 0
        result["runway_label"] = "already short before ongoing growth"
        return result

    if unbounded_gb_per_day <= 0:
        result["verdict"] = "excess"
        result["excess_gb"] = round(remaining_free_gb, 1)
        result["runway_days"] = None
        result["runway_label"] = "storage stabilizes once retention limits are reached"
        return result

    runway_days = remaining_free_gb / unbounded_gb_per_day
    result["verdict"] = "runway"
    result["runway_days"] = round(runway_days, 1)
    result["runway_label"] = _human_duration(runway_days)
    return result


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
