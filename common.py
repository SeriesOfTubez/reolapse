"""Shared config loading and storage-path helpers for the timelapse scripts.

Secrets never live in config.yaml. Any ${VAR} in the config is substituted
from the environment at load time, and a .env file next to this module is
loaded first (without overriding real environment variables, so Docker /
systemd values win). See config.example.yaml and .env.example.
"""

import os
import re
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional if you set env vars another way
    def load_dotenv(*_args, **_kwargs):
        return False

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_ROOT / "config.yaml"

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


def snapshots_dir(cfg) -> Path:
    return cfg["storage"]["root"] / "snapshots"


def videos_dir(cfg) -> Path:
    return cfg["storage"]["root"] / "videos"


def yearly_frames_dir(cfg) -> Path:
    return cfg["storage"]["root"] / "yearly_frames"
