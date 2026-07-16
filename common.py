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


def effective_daylight_window(global_dl, camera_dl):
    """Merge global and camera-specific daylight window settings.
    
    Camera settings override global ones when present.
    """
    if not camera_dl:
        return global_dl
    
    # Start with global settings as defaults
    effective = global_dl.copy() if global_dl else {}
    
    # Override with camera-specific settings
    if camera_dl.get("enabled") is not None:
        effective["enabled"] = camera_dl["enabled"]
    if camera_dl.get("mode") is not None:
        effective["mode"] = camera_dl["mode"]
    if camera_dl.get("buffer_minutes") is not None:
        effective["buffer_minutes"] = camera_dl["buffer_minutes"]
        
    return effective


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
