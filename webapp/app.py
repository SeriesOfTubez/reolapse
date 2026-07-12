#!/usr/bin/env python3
"""Tiny Flask app for browsing timelapse videos.

    python webapp/app.py            # serves http://localhost:8080
"""

import argparse
import concurrent.futures
import json
import re
import shutil
import socket
import sys
from pathlib import Path

import requests
import urllib3
import yaml
from flask import Flask, abort, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DEFAULT_CONFIG, load_config, videos_dir  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VIDEO_TYPES = ("daily", "yearly", "events")
US_ZIP_RE = re.compile(r"^\d{5}$")
ENV_VAR_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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

REQUIRED_SECTIONS = ("capture", "storage", "daily_video", "yearly")


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


def validate_config(cfg):
    """Light structural validation before writing config.yaml. Not a full
    schema check — just enough to catch the mistakes that would otherwise
    break capture/build silently or, worse, leak a real secret into the file.
    """
    problems = []
    if not isinstance(cfg, dict):
        return ["top level must be a mapping"]

    cameras = cfg.get("cameras")
    if not isinstance(cameras, list):
        problems.append("cameras must be a list")
    else:
        seen_names = set()
        for i, cam in enumerate(cameras):
            if not isinstance(cam, dict):
                problems.append(f"cameras[{i}] must be a mapping")
                continue
            for key in ("name", "host", "channel", "username", "password"):
                if not cam.get(key) and cam.get(key) != 0:
                    problems.append(f"cameras[{i}] missing '{key}'")
            name = str(cam.get("name", ""))
            if name and not CAMERA_NAME_RE.match(name):
                problems.append(f"cameras[{i}].name {name!r} must be letters/digits/dashes only")
            if name in seen_names:
                problems.append(f"cameras[{i}].name {name!r} is used more than once")
            seen_names.add(name)
            pw = str(cam.get("password", ""))
            if pw and not ENV_VAR_RE.match(pw):
                problems.append(
                    f"cameras[{i}].password must reference an environment variable "
                    f"like \"${{VAR_NAME}}\" (never a literal secret) — set the real "
                    f"value in .env. Got: {pw!r}"
                )

    for section in REQUIRED_SECTIONS:
        if not isinstance(cfg.get(section), dict):
            problems.append(f"missing or invalid '{section}' section")

    accent = (cfg.get("webapp") or {}).get("accent_color")
    if accent and accent not in ACCENT_COLORS:
        problems.append(f"webapp.accent_color {accent!r} must be one of {sorted(ACCENT_COLORS)}")

    return problems


def local_subnet_prefix():
    """Best-effort /24 prefix of this machine's primary local IP, e.g.
    "192.168.1". A UDP "connect" never actually sends a packet — it just
    asks the OS to pick the outbound interface/route, which is all this
    needs, so it works even with no real internet access.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


def probe_reolink_ip(ip, port_timeout=0.6, http_timeout=3):
    """Unauthenticated check: does something Reolink-API-shaped answer here?

    A real Reolink device answers GetDevInfo with a structured JSON error
    ("please login first") even with no credentials — that shape is what we
    match on, not success, since we don't have credentials yet at this stage.
    """
    for scheme, port in (("https", 443), ("http", 80)):
        try:
            with socket.create_connection((ip, port), timeout=port_timeout):
                pass
        except OSError:
            continue
        try:
            # verify=False: Reolink devices ship self-signed certs (same
            # tradeoff as capture.py's verify_ssl option, documented in the
            # README) - and this is a discovery probe to an IP with no prior
            # trust anyway, on the LAN this app already requires.
            r = requests.post(  # nosemgrep: disabled-cert-validation
                f"{scheme}://{ip}/cgi-bin/api.cgi?cmd=GetDevInfo",
                json=[{"cmd": "GetDevInfo", "action": 0, "param": {}}],
                timeout=http_timeout, verify=False,
            )
            body = r.json()
            if isinstance(body, list) and body and body[0].get("cmd") == "GetDevInfo":
                return {"ip": ip, "https": scheme == "https"}
        except Exception:
            pass
    return None


def identify_reolink_device(host, username, password, https=True, timeout=10):
    """Authenticated GetDevInfo (+ GetChannelstatus for NVRs) — this is the
    step that actually reveals model/name/channels, which the unauthenticated
    scan can't. Only called when the user supplies credentials for one
    specific discovered IP, never during the scan itself.
    """
    scheme = "https" if https else "http"
    base = f"{scheme}://{host}/cgi-bin/api.cgi"

    def call(cmd, param, token=None):
        params = {"cmd": cmd, "user": username, "password": password}
        if token:
            params["token"] = token
        # verify=False: same self-signed-cert tradeoff as above.
        r = requests.post(  # nosemgrep: disabled-cert-validation
            base, params=params,
            json=[{"cmd": cmd, "action": 0, "param": param}],
            timeout=timeout, verify=False)
        return r.json()[0]

    token = None
    try:
        res = call("Login", {"User": {"userName": username, "password": password}})
        if res.get("code") != 0:
            return {"error": res.get("error", {}).get("detail", "login failed")}
        token = res["value"]["Token"]["name"]

        info = call("GetDevInfo", {}, token)["value"]["DevInfo"]
        result = {
            "model": info.get("model"),
            "name": info.get("name"),
            "channel_count": info.get("channelNum", 1),
            "channels": [],
        }
        if result["channel_count"] and result["channel_count"] > 1:
            status = call("GetChannelstatus", {}, token)
            for ch in status.get("value", {}).get("status", []):
                if ch.get("online"):
                    result["channels"].append({"channel": ch["channel"], "name": ch.get("name", "")})
        return result
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if token:
            try:
                call("Logout", {}, token)
            except Exception:
                pass


def create_app(cfg, config_path=None):
    app = Flask(__name__, static_folder="static")
    # Mutable so a successful config save can update what every route sees
    # without restarting this process. capture.py/build_timelapse.py are
    # separate processes and still need a manual restart to pick up changes.
    state = {"cfg": cfg, "path": Path(config_path) if config_path else None}

    @app.get("/")
    def index():
        cfg = state["cfg"]
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
        cfg = state["cfg"]
        video_root = videos_dir(cfg)
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
        stats_path = state["cfg"]["storage"]["root"] / "storage_stats.json"
        if not stats_path.exists():
            return jsonify({"generated_at": None, "cameras": {}, "system": {}})
        return jsonify(json.loads(stats_path.read_text(encoding="utf-8")))

    @app.get("/api/config")
    def get_config():
        if not state["path"]:
            return jsonify({"error": "no config file path known"}), 400
        if not state["path"].exists():
            return jsonify({"error": f"{state['path']} does not exist"}), 404
        # Parse the file directly (NOT common.load_config), so ${VAR} secret
        # placeholders come through unresolved and real secret values are
        # never sent to the browser.
        raw = state["path"].read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw) or {}
        return jsonify({
            "config": parsed,
            "path": str(state["path"]),
            "accent_colors": sorted(ACCENT_COLORS),
        })

    @app.post("/api/config")
    def save_config():
        if not state["path"]:
            return jsonify({"error": "no config file path known"}), 400
        new_cfg = request.get_json(force=True, silent=True)
        if not isinstance(new_cfg, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        problems = validate_config(new_cfg)
        if problems:
            return jsonify({"error": "validation failed", "problems": problems}), 400

        path = state["path"]
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            if path.exists():
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(new_cfg, f, sort_keys=False, default_flow_style=False,
                                allow_unicode=True)
        except OSError as exc:
            return jsonify({"error": f"failed to write {path}: {exc}"}), 500

        note = (
            "Saved. This page now reflects the change, but capture.py and "
            "build_timelapse.py are separate processes — restart the "
            "reolapse-capture/reolapse-web services (or `docker compose "
            "restart`) for them to pick it up too. "
            f"Previous version backed up to {backup.name}. Comments and "
            "formatting in config.yaml are not preserved by this editor."
        )
        try:
            state["cfg"] = load_config(str(path))
        except SystemExit as exc:
            return jsonify({"ok": True, "note": note,
                            "warning": f"saved, but this page failed to reload it: {exc}"})
        return jsonify({"ok": True, "note": note})

    @app.post("/api/discover")
    def discover():
        body = request.get_json(silent=True) or {}
        prefix = body.get("subnet") or local_subnet_prefix()
        ips = [f"{prefix}.{i}" for i in range(1, 255)]
        found = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            for result in pool.map(probe_reolink_ip, ips):
                if result:
                    found.append(result)
        found.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
        return jsonify({"subnet": prefix, "found": found})

    @app.post("/api/discover/identify")
    def discover_identify():
        body = request.get_json(silent=True) or {}
        host = body.get("host")
        username = body.get("username")
        password = body.get("password")
        if not (host and username and password):
            return jsonify({"error": "host, username, and password are required"}), 400
        result = identify_reolink_device(host, username, password, https=bool(body.get("https", True)))
        if "error" in result:
            return jsonify(result), 502
        return jsonify(result)

    @app.get("/videos/<camera>/<vtype>/<name>")
    def serve_video(camera, vtype, name):
        if vtype not in VIDEO_TYPES or not name.endswith(".mp4"):
            abort(404)
        # send_from_directory rejects path traversal and handles Range
        # requests, which the <video> element needs for seeking.
        return send_from_directory(videos_dir(state["cfg"]) / camera / vtype, name)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args()

    config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG
    cfg = load_config(str(config_path))
    web = cfg.get("webapp", {})
    app = create_app(cfg, config_path)
    app.run(host=web.get("host", "127.0.0.1"), port=web.get("port", 8080), threaded=True)


if __name__ == "__main__":
    main()
