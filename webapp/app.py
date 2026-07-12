#!/usr/bin/env python3
"""Tiny Flask app for browsing timelapse videos.

    python webapp/app.py            # serves http://localhost:8080
"""

import argparse
import json
import re
import sys
from pathlib import Path

from flask import Flask, abort, jsonify, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_config, videos_dir  # noqa: E402

VIDEO_TYPES = ("daily", "yearly", "events")
US_ZIP_RE = re.compile(r"^\d{5}$")

# name -> (accent, accent-strong, accent-quiet rgba). All calibrated to
# roughly the same lightness/saturation as the amber default so a single
# fixed dark --accent-contrast text color stays readable on every option.
ACCENT_COLORS = {
    "amber":  ("#f2a94e", "#f79a3e", "rgba(242, 169, 78, 0.14)"),
    "green":  ("#6fcf97", "#57bd82", "rgba(111, 207, 151, 0.14)"),
    "blue":   ("#6fa8f5", "#5b93e8", "rgba(111, 168, 245, 0.14)"),
    "red":    ("#f2705b", "#e85940", "rgba(242, 112, 91, 0.14)"),
    "purple": ("#b18cf2", "#9d72e8", "rgba(177, 140, 242, 0.14)"),
    "yellow": ("#e8d44a", "#dcc430", "rgba(232, 212, 74, 0.14)"),
}


def config_warnings(cfg):
    """Cheap, network-free config sanity checks surfaced in the web UI."""
    warnings = []
    ecfg = cfg.get("events") or {}
    if ecfg.get("weather_enabled"):
        zip_code = str(ecfg.get("zip") or ecfg.get("zip_code") or "").strip()
        has_latlon = ecfg.get("latitude") is not None and ecfg.get("longitude") is not None
        if not has_latlon and not zip_code:
            warnings.append(
                "events.weather_enabled is true but no location is set — add "
                "events.zip or events.latitude/longitude. Storm/snow tagging "
                "is currently disabled."
            )
        elif zip_code and not has_latlon and not US_ZIP_RE.match(zip_code):
            warnings.append(
                f'events.zip "{zip_code}" doesn\'t look like a valid 5-digit '
                "US ZIP code — weather tagging may not be resolving a location."
            )
    return warnings


def create_app(cfg):
    app = Flask(__name__, static_folder="static")
    video_root = videos_dir(cfg)

    @app.get("/")
    def index():
        html = (Path(app.static_folder) / "index.html").read_text(encoding="utf-8")
        name = (cfg.get("webapp") or {}).get("accent_color", "amber")
        accent, strong, quiet = ACCENT_COLORS.get(name, ACCENT_COLORS["amber"])
        # A tiny override block placed right before </head>: same specificity
        # as the stylesheet's own :root block, so it wins by cascade order
        # without touching the static file or needing a templating engine.
        override = (f"<style>:root {{ --accent: {accent}; "
                    f"--accent-strong: {strong}; --accent-quiet: {quiet}; }}</style>")
        html = html.replace("</head>", override + "</head>")
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.get("/api/videos")
    def list_videos():
        videos = []
        if video_root.exists():
            for cam_dir in sorted(video_root.iterdir()):
                if not cam_dir.is_dir():
                    continue
                for vtype in VIDEO_TYPES:
                    type_dir = cam_dir / vtype
                    if not type_dir.is_dir():
                        continue
                    for f in type_dir.glob("*.mp4"):
                        stat = f.stat()
                        videos.append({
                            "camera": cam_dir.name,
                            "type": vtype,
                            "label": f.stem,
                            "url": f"/videos/{cam_dir.name}/{vtype}/{f.name}",
                            "size_mb": round(stat.st_size / 1e6, 1),
                        })
        videos.sort(key=lambda v: (v["camera"], v["type"], v["label"]), reverse=True)
        return jsonify({
            "cameras": sorted({v["camera"] for v in videos}),
            "videos": videos,
            "warnings": config_warnings(cfg),
        })

    @app.get("/api/storage")
    def storage():
        stats_path = cfg["storage"]["root"] / "storage_stats.json"
        if not stats_path.exists():
            return jsonify({"generated_at": None, "cameras": {}, "system": {}})
        return jsonify(json.loads(stats_path.read_text(encoding="utf-8")))

    @app.get("/videos/<camera>/<vtype>/<name>")
    def serve_video(camera, vtype, name):
        if vtype not in VIDEO_TYPES or not name.endswith(".mp4"):
            abort(404)
        # send_from_directory rejects path traversal and handles Range
        # requests, which the <video> element needs for seeking.
        return send_from_directory(video_root / camera / vtype, name)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    web = cfg.get("webapp", {})
    app = create_app(cfg)
    app.run(host=web.get("host", "127.0.0.1"), port=web.get("port", 8080), threaded=True)


if __name__ == "__main__":
    main()
