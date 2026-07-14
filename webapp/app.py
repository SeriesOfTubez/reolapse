#!/usr/bin/env python3
"""Tiny Flask app for browsing timelapse videos.

    python webapp/app.py            # serves http://localhost:8080
"""

import argparse
import concurrent.futures
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
import urllib3
import yaml
from flask import Flask, abort, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import APP_VERSION, DEFAULT_CONFIG, load_config, videos_dir  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VIDEO_TYPES = ("daily", "yearly", "events")
US_ZIP_RE = re.compile(r"^\d{5}$")
ENV_VAR_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Convention (not enforced by ENV_VAR_RE, which accepts any valid env var
# name): password vars are discoverable in the Config page's dropdown when
# they're named REOLINK_PASSWORD or REOLINK_PASSWORD_<anything>.
PASSWORD_VAR_RE = re.compile(r"^REOLINK_PASSWORD(_\w+)?$")

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

# Keep in sync with capture.py's MIN_INTERVAL_SECONDS — the floor on poll rate.
MIN_INTERVAL_SECONDS = 10

# --- Config-page authentication --------------------------------------------
# A single optional passcode (no username) gates the Config page and its
# write/scan endpoints. It's opt-in: with no passcode set, everything behaves
# exactly as before and the whole app stays unauthenticated. See the Security
# section of the README.
SESSION_COOKIE = "reolapse_session"
SESSION_TTL = 12 * 3600           # a login lasts 12h, then re-enter the passcode
MIN_PASSCODE_LEN = 4
AUTH_MAX_FAILURES = 10            # failed logins allowed within the window...
AUTH_FAILURE_WINDOW = 300        # ...before the endpoint locks out (seconds)
# scrypt work factors. n*r*128 bytes of memory (~16 MiB here); maxmem must
# clear that. Encoded into each hash so these can change without breaking
# passcodes set under the old values.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_passcode(passcode):
    """One-way hash of a Config-page passcode for storage in config.yaml.

    A hash is safe to keep in config.yaml (unlike a camera password, which
    must stay reversible to authenticate to the device): it can't be used to
    log in and reversing it means an offline crack. Salt and work factors are
    encoded in the returned string so verify() is self-describing.
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_passcode(passcode, stored):
    """Constant-time check of a passcode against a stored hash_passcode() str.
    Returns False for any malformed/unknown hash rather than raising."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(passcode.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(expected),
                            maxmem=SCRYPT_MAXMEM)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def available_password_vars():
    """Names (never values) of loaded env vars that look like camera
    passwords, for the Config page's password dropdown. .env is already
    loaded into os.environ by load_config() at process startup, so this is
    just a filtered read of what's already there — no file access, no
    secret ever leaves this function.
    """
    return sorted(k for k in os.environ if PASSWORD_VAR_RE.match(k))


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

    pw_hash = (cfg.get("webapp") or {}).get("config_passcode_hash")
    if pw_hash is not None and not isinstance(pw_hash, str):
        problems.append("webapp.config_passcode_hash must be a string (set it via the Config page, not by hand)")

    # Minimum poll interval — faster than this risks overloading the camera/NVR.
    for path, val in (("capture.interval_seconds", (cfg.get("capture") or {}).get("interval_seconds")),
                      ("events.burst_interval_seconds", (cfg.get("events") or {}).get("burst_interval_seconds"))):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if val < MIN_INTERVAL_SECONDS:
            problems.append(f"{path} must be at least {MIN_INTERVAL_SECONDS} seconds "
                            "— faster polling can overload the camera/NVR")

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


CAPTURE_UNIT = "reolapse-capture.service"
WEB_UNIT = "reolapse-web.service"


def restart_capture_service():
    """Synchronous restart of the capture service — safe to do inline since
    we're not restarting the process handling this request.

    Requires a narrowly-scoped passwordless sudo rule for exactly
    `systemctl restart reolapse-capture.service` (see README) — nothing
    broader. `sudo -n` fails immediately rather than hanging if that isn't
    configured, so this degrades to a clear error instead of a stuck request.
    """
    if not shutil.which("systemctl"):
        return False, ("systemctl not found — restart-from-UI only works on a systemd "
                       "Linux host, not Docker. Run `docker compose restart` instead.")
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", CAPTURE_UNIT],
                       check=True, capture_output=True, timeout=15, text=True)
        return True, f"{CAPTURE_UNIT} restarted"
    except subprocess.CalledProcessError as exc:
        return False, (f"Failed to restart {CAPTURE_UNIT}: {(exc.stderr or '').strip() or exc}. "
                       "Passwordless sudo for this exact command may not be configured — see README.")
    except Exception as exc:
        return False, f"Failed to restart {CAPTURE_UNIT}: {exc}"


def schedule_self_restart(delay=1.5):
    """Restart the web service from a background thread, after a short delay
    so the HTTP response for this request has time to reach the client first.
    Nothing meaningful to report afterward either way — the process ends.
    """
    def _do_restart():
        time.sleep(delay)
        try:
            subprocess.run(["sudo", "-n", "systemctl", "restart", WEB_UNIT],
                           timeout=15, capture_output=True)
        except Exception:
            pass
    threading.Thread(target=_do_restart, daemon=True).start()


def create_app(cfg, config_path=None):
    app = Flask(__name__, static_folder="static")
    # Mutable so a successful config save can update what every route sees
    # without restarting this process. capture.py/build_timelapse.py are
    # separate processes and still need a manual restart to pick up changes.
    state = {"cfg": cfg, "path": Path(config_path) if config_path else None}

    # Session tokens live in-process (single threaded WSGI server): a plain
    # dict of token -> expiry, plus a sliding window of failed-login times for
    # brute-force throttling. Restarting the web service clears both, logging
    # everyone out — acceptable, and it means no secret_key to persist.
    sessions = {}
    auth_failures = []
    auth_lock = threading.Lock()

    def passcode_hash():
        return ((state["cfg"].get("webapp") or {}).get("config_passcode_hash") or "").strip()

    def auth_enabled():
        return bool(passcode_hash())

    def new_session():
        token = secrets.token_urlsafe(32)
        with auth_lock:
            sessions[token] = time.time() + SESSION_TTL
        return token

    def session_valid():
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        now = time.time()
        with auth_lock:
            for t in [k for k, exp in sessions.items() if exp < now]:
                sessions.pop(t, None)
            exp = sessions.get(token)
        return exp is not None and exp >= now

    def clear_sessions():
        with auth_lock:
            sessions.clear()

    def set_session_cookie(resp, token):
        # No secure=True: LAN deployments run plain HTTP with no TLS. SameSite
        # =Lax still blocks the cookie on cross-site POST/fetch, so a malicious
        # off-origin page can't ride the session to hit the write endpoints.
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
                        httponly=True, samesite="Lax")
        return resp

    def require_config_auth(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if auth_enabled() and not session_valid():
                return jsonify({"error": "authentication required for the Config page",
                                "auth_required": True}), 401
            return fn(*args, **kwargs)
        return wrapper

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
            "version": APP_VERSION,
        })

    @app.get("/api/storage")
    def storage():
        stats_path = state["cfg"]["storage"]["root"] / "storage_stats.json"
        if not stats_path.exists():
            return jsonify({"generated_at": None, "cameras": {}, "system": {}})
        return jsonify(json.loads(stats_path.read_text(encoding="utf-8")))

    @app.get("/api/config")
    @require_config_auth
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
        # The passcode hash never goes to the browser (it's managed through the
        # dedicated /api/auth/passcode endpoint, not the config editor).
        if isinstance(parsed.get("webapp"), dict):
            parsed["webapp"].pop("config_passcode_hash", None)
        return jsonify({
            "config": parsed,
            "path": str(state["path"]),
            "accent_colors": sorted(ACCENT_COLORS),
            "password_vars": available_password_vars(),
        })

    @app.post("/api/config")
    @require_config_auth
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
        # get_config strips the passcode hash before sending config to the
        # browser, so the saved payload never carries it — re-inject the
        # current on-disk hash so a config save doesn't silently disable auth.
        try:
            on_disk = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            existing_hash = (on_disk.get("webapp") or {}).get("config_passcode_hash")
        except (OSError, yaml.YAMLError):
            existing_hash = None
        if existing_hash and isinstance(new_cfg.get("webapp"), dict):
            new_cfg["webapp"]["config_passcode_hash"] = existing_hash
        elif existing_hash:
            new_cfg.setdefault("webapp", {})["config_passcode_hash"] = existing_hash

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
    @require_config_auth
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
    @require_config_auth
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

    @app.post("/api/restart")
    @require_config_auth
    def restart_services():
        ok, message = restart_capture_service()
        if not ok:
            return jsonify({"error": message}), 500
        # The web service restarting itself would kill this very request if
        # done synchronously here — deferred so this response reaches the
        # client first. The connection will still drop a moment later; the
        # frontend treats that as expected, not an error.
        schedule_self_restart()
        return jsonify({
            "ok": True,
            "note": f"{message}. This web service is restarting too — the page "
                    "will disconnect for a few seconds, then reload.",
        })

    @app.get("/api/auth")
    def auth_status():
        # Always open: the frontend calls this to decide whether to show the
        # passcode form. Reveals only whether a gate exists, never the hash.
        return jsonify({"enabled": auth_enabled(), "authed": session_valid()})

    @app.post("/api/auth")
    def auth_login():
        if not auth_enabled():
            return jsonify({"error": "no passcode is set; the Config page is open"}), 400
        now = time.time()
        with auth_lock:
            auth_failures[:] = [t for t in auth_failures if now - t < AUTH_FAILURE_WINDOW]
            if len(auth_failures) >= AUTH_MAX_FAILURES:
                retry = int(AUTH_FAILURE_WINDOW - (now - auth_failures[0])) + 1
                return jsonify({"error": f"too many attempts — wait {retry}s and try again",
                                "retry_after": retry}), 429
        body = request.get_json(silent=True) or {}
        if not verify_passcode(str(body.get("passcode") or ""), passcode_hash()):
            time.sleep(0.4)   # blunt scripted guessing; harmless at this scale
            with auth_lock:
                auth_failures.append(now)
            return jsonify({"error": "incorrect passcode"}), 401
        with auth_lock:
            auth_failures.clear()
        return set_session_cookie(jsonify({"ok": True}), new_session())

    @app.post("/api/auth/logout")
    def auth_logout():
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with auth_lock:
                sessions.pop(token, None)
        resp = jsonify({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, samesite="Lax")
        return resp

    @app.post("/api/auth/passcode")
    def set_passcode():
        # Setting the FIRST passcode is open — it matches the currently-open
        # page (same trust model as everything else being unauthenticated).
        # Changing or clearing an existing one requires a valid session.
        if auth_enabled() and not session_valid():
            return jsonify({"error": "authentication required", "auth_required": True}), 401
        if not state["path"]:
            return jsonify({"error": "no config file path known"}), 400

        body = request.get_json(silent=True) or {}
        if body.get("clear"):
            new_hash = ""
        else:
            new_passcode = str(body.get("passcode") or "")
            if len(new_passcode) < MIN_PASSCODE_LEN:
                return jsonify({"error": f"passcode must be at least {MIN_PASSCODE_LEN} characters"}), 400
            new_hash = hash_passcode(new_passcode)

        path = state["path"]
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            return jsonify({"error": f"failed to read {path}: {exc}"}), 500
        webapp_cfg = parsed.get("webapp")
        if not isinstance(webapp_cfg, dict):
            webapp_cfg = {}
            parsed["webapp"] = webapp_cfg
        if new_hash:
            webapp_cfg["config_passcode_hash"] = new_hash
        else:
            webapp_cfg.pop("config_passcode_hash", None)

        backup = path.with_suffix(path.suffix + ".bak")
        try:
            if path.exists():
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(parsed, f, sort_keys=False, default_flow_style=False,
                                allow_unicode=True)
        except OSError as exc:
            return jsonify({"error": f"failed to write {path}: {exc}"}), 500
        try:
            state["cfg"] = load_config(str(path))
        except SystemExit as exc:
            return jsonify({"error": f"saved, but this page failed to reload config: {exc}"}), 500

        # Any passcode change invalidates existing sessions. Setting one from an
        # open page logs the setter straight in; clearing drops the cookie.
        clear_sessions()
        if new_hash:
            return set_session_cookie(jsonify({"ok": True, "enabled": True}), new_session())
        resp = jsonify({"ok": True, "enabled": False})
        resp.delete_cookie(SESSION_COOKIE, samesite="Lax")
        return resp

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
    print(f"ReoLapse web v{APP_VERSION}")
    app = create_app(cfg, config_path)
    app.run(host=web.get("host", "127.0.0.1"), port=web.get("port", 8080), threaded=True)


if __name__ == "__main__":
    main()
