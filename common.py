"""Shared config loading and storage-path helpers for the timelapse scripts.

Secrets never live in config.yaml. Any ${VAR} in the config is substituted
from the environment at load time, and a .env file next to this module is
loaded first (without overriding real environment variables, so Docker /
systemd values win). See config.example.yaml and .env.example.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional if you set env vars another way
    def load_dotenv(*_args, **_kwargs):
        return False

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_ROOT / "config.yaml"


def tzinfo_for(tzname):
    """A tzinfo for an IANA name (e.g. 'America/Chicago'), or None if the name
    is empty/unknown or zoneinfo/tzdata is unavailable — in which case callers
    fall back to host-local time. Never raises."""
    if not tzname or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(str(tzname).strip())
    except Exception:
        return None


def local_now(tz=None):
    """Current time. With a tzinfo, an aware datetime in that zone; otherwise
    host-local naive time (the original, pre-timezone-config behavior). Using a
    configured zone means a misconfigured host clock can't shift capture days."""
    return datetime.now(tz) if tz is not None else datetime.now()


def local_today(tz=None):
    return local_now(tz).date()


def app_version():
    """The release version from the VERSION file, or 'unknown' if it's missing
    (e.g. a partial copy). Used for logging and the web UI's version display."""
    try:
        return (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


APP_VERSION = app_version()

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value):
    """Recursively replace ${VAR} in strings with environment values."""
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, str):
        def repl(match):
            name = match.group(1)
            if name not in os.environ:
                raise SystemExit(
                    f"Config references ${{{name}}} but that variable is not set.\n"
                    f"Add it to your .env file (see .env.example) or export it."
                )
            return os.environ[name]
        return _ENV_PATTERN.sub(repl, value)
    return value


def load_config(config_path=None):
    load_dotenv(APP_ROOT / ".env", override=False)

    config_path = Path(config_path or DEFAULT_CONFIG).resolve()
    if not config_path.exists():
        raise SystemExit(
            f"Config file not found: {config_path}\n"
            "Copy config.example.yaml to config.yaml and edit it first."
        )
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg = _interpolate(cfg)

    root = Path(cfg["storage"]["root"])
    if not root.is_absolute():
        root = (config_path.parent / root).resolve()
    cfg["storage"]["root"] = root
    return cfg


DAYLIGHT_KEYS = ("enabled", "mode", "buffer_minutes")


def camera_daylight_config(cfg, cam) -> dict:
    """A camera's effective daylight window settings.

    Each key falls back to the global capture.daylight_window independently, so
    a camera can flip just the mode and inherit the buffer. A camera may also
    enable its own window while the global one is off (the "one night camera in
    an otherwise daytime setup" case) — the two are resolved separately.
    Always returns a fresh dict; callers must never alias the global config.
    """
    merged = dict((cfg.get("capture") or {}).get("daylight_window") or {})
    for key, value in ((cam.get("daylight_window") or {}).items()):
        if key in DAYLIGHT_KEYS and value is not None:
            merged[key] = value
    return merged


def camera_interval_seconds(cfg, cam, minimum=10) -> int:
    """A camera's capture interval, falling back to the global one.

    Lets a night camera poll less often than the daytime ones, so covering the
    dark hours doesn't double what the setup writes to disk.
    """
    value = cam.get("interval_seconds")
    if value is None:
        value = (cfg.get("capture") or {}).get("interval_seconds")
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = minimum
    return max(minimum, value)


def camera_events_enabled(cfg, cam) -> bool:
    """Whether a camera participates in weather/lunar events at all.

    False means it ignores them entirely: it holds its own interval through a
    burst instead of speeding up, and no event videos are built from its
    frames. That's the right setting for a camera the weather isn't visible
    from — indoors, a doorway, a tight framing — where burst frames are just
    disk and an event clip is a video of nothing happening.

    Defaults to True; omit the key to participate normally. Takes cfg for
    signature symmetry with the other per-camera helpers (there's no global
    counterpart to fall back to — events are configured under `events`).
    """
    value = cam.get("events_enabled")
    return True if value is None else bool(value)


def camera_include_events_in_daily(cfg, cam) -> bool:
    """Whether a camera's event-burst frames go into its daily video.

    During a storm/snow burst, capture drops to events.burst_interval_seconds
    and those minutes land in the same day folder as everything else, so the
    daily video crawls through the storm at a fraction of its normal pace.
    False (the default) leaves those minutes out of the daily .mp4 only — the
    event clip, the yearly frame archive, and the frames on disk are untouched.

    Per-camera value wins when present; otherwise falls back to the global
    daily_video.include_events, same shape as camera_interval_seconds.
    """
    value = cam.get("include_events_in_daily")
    if value is None:
        value = (cfg.get("daily_video") or {}).get("include_events")
    return bool(value)


def build_status_path(cfg) -> Path:
    return cfg["storage"]["root"] / "build_status.json"


def read_build_status(cfg, stale_seconds=3600) -> dict:
    """Current video-build status, written by build_timelapse and read by the
    web UI (they share the data dir). Returns {"state": "idle"} if nothing has
    run, and treats a "running" status older than stale_seconds as idle so a
    build killed mid-run (no clean exit) doesn't leave the UI stuck."""
    try:
        data = json.loads(build_status_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle"}
    if data.get("state") == "running" and \
            time.time() - data.get("started_epoch", 0) > stale_seconds:
        return {"state": "idle", "last": data.get("last")}
    return data


def snapshots_dir(cfg) -> Path:
    return cfg["storage"]["root"] / "snapshots"


def videos_dir(cfg) -> Path:
    return cfg["storage"]["root"] / "videos"


def yearly_frames_dir(cfg) -> Path:
    return cfg["storage"]["root"] / "yearly_frames"
