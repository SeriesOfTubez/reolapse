#!/usr/bin/env bash
#
# ReoLapse easy installer — Debian/Ubuntu + systemd.
#
#   curl -fsSL https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/install.sh | bash
#
# Piping a script straight into your shell means running it sight-unseen. Fair
# enough to read it first (always a good habit for any curl|bash):
#
#   curl -fsSL https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/install.sh -o install.sh
#   less install.sh
#   bash install.sh
#
# Run as a normal user with sudo access — NOT as root (the services run as your
# user; the script calls sudo only where it must). It won't start capturing
# until you've filled in config.yaml and .env — it prints the next steps.
#
# Env-var options:
#   REOLAPSE_DIR=/opt/reolapse     where to install
#   REOLAPSE_BRANCH=main           branch/tag to install
#   REOLAPSE_SKIP_SUDOERS=1        skip the Restart-button sudo rule
#
# Every line below lives in a function called from main() at the very end, so a
# truncated download can't execute a half-command.

set -euo pipefail

REPO_URL="https://github.com/SeriesOfTubez/reolapse.git"
DEST="${REOLAPSE_DIR:-/opt/reolapse}"
BRANCH="${REOLAPSE_BRANCH:-main}"

if [ -t 1 ]; then
  c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
else
  c_bold=""; c_grn=""; c_ylw=""; c_red=""; c_rst=""
fi
info() { printf '%s==>%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s!!  %s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%sxx  %s%s\n' "$c_red" "$*" "$c_rst" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

preflight() {
  if [ "$(id -u)" -eq 0 ]; then
    err "Run this as a normal user with sudo — not as root."
    err "ReoLapse's services should not run as root; the script uses sudo where needed."
    exit 1
  fi
  have sudo    || { err "sudo is required."; exit 1; }
  have apt-get || { err "This installer targets Debian/Ubuntu (apt). Use the README's manual steps on other distros."; exit 1; }
  [ -d /run/systemd/system ] || { err "systemd not detected. On Docker, use the 'Quick start (Docker)' method instead."; exit 1; }
}

install_packages() {
  info "Installing system packages (git, ffmpeg, python3, venv)…"
  sudo apt-get update -y
  sudo apt-get install -y git ffmpeg python3 python3-venv
}

fetch_source() {
  sudo mkdir -p "$DEST"
  sudo chown -R "$USER:$(id -gn)" "$DEST"
  if [ -d "$DEST/.git" ]; then
    info "Updating existing checkout at $DEST…"
    git -C "$DEST" fetch --depth 1 origin "$BRANCH"
    git -C "$DEST" checkout -q "$BRANCH"
    git -C "$DEST" reset --hard "origin/$BRANCH"
  elif [ -z "$(ls -A "$DEST" 2>/dev/null)" ]; then
    info "Cloning ReoLapse ($BRANCH) into $DEST…"
    git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$DEST"
  else
    err "$DEST exists, isn't a ReoLapse checkout, and isn't empty. Move it or set REOLAPSE_DIR."
    exit 1
  fi
}

setup_venv() {
  info "Creating the Python venv and installing dependencies…"
  python3 -m venv "$DEST/venv"
  "$DEST/venv/bin/pip" install --quiet --upgrade pip
  "$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"
}

setup_config() {
  if [ ! -f "$DEST/config.yaml" ]; then
    cp "$DEST/config.example.yaml" "$DEST/config.yaml"
    info "Created config.yaml from the example — edit it for your cameras."
  else
    warn "config.yaml already exists — left untouched."
  fi
  if [ ! -f "$DEST/.env" ]; then
    cp "$DEST/.env.example" "$DEST/.env"
    info "Created .env from the example — set REOLINK_PASSWORD."
  else
    warn ".env already exists — left untouched."
  fi
  chmod 600 "$DEST/.env" "$DEST/config.yaml"
}

install_units() {
  info "Installing systemd units (User=$USER, dir=$DEST)…"
  local tmp; tmp="$(mktemp -d)"
  for f in "$DEST"/deploy/reolapse-*.service "$DEST"/deploy/reolapse-*.timer; do
    sed -e "s|^User=ubuntu$|User=$USER|" -e "s|/opt/reolapse|$DEST|g" \
        "$f" > "$tmp/$(basename "$f")"
  done
  sudo cp "$tmp"/reolapse-*.service "$tmp"/reolapse-*.timer /etc/systemd/system/
  rm -rf "$tmp"
  sudo systemctl daemon-reload
}

install_sudoers() {
  if [ "${REOLAPSE_SKIP_SUDOERS:-0}" = "1" ]; then
    warn "Skipping the Restart-button sudo rule (REOLAPSE_SKIP_SUDOERS=1)."
    return
  fi
  local sctl; sctl="$(command -v systemctl)"
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# ReoLapse: let the service user restart ONLY its own services without a
# password, so the Config page's "Restart services" button works. Nothing else.
$USER ALL=(root) NOPASSWD: $sctl restart reolapse-capture.service, $sctl restart reolapse-web.service
EOF
  if sudo visudo -cf "$tmp" >/dev/null; then
    sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/reolapse
    info "Installed /etc/sudoers.d/reolapse — the Restart button will work."
  else
    warn "Generated sudoers rule failed validation; not installing it. The Restart"
    warn "button just won't work until you add the rule by hand (see the README)."
  fi
  rm -f "$tmp"
}

enable_services() {
  info "Enabling the nightly build timers…"
  sudo systemctl enable --now reolapse-daily.timer reolapse-yearly.timer
  info "Enabling the capture service (NOT starting it — it needs your config first)…"
  sudo systemctl enable reolapse-capture.service
  info "Starting the web UI…"
  if ! sudo systemctl enable --now reolapse-web.service; then
    warn "Web service didn't start cleanly — check: journalctl -u reolapse-web.service -n 30"
  fi
}

print_next_steps() {
  local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '\n%sReoLapse is installed.%s\n\n' "$c_bold" "$c_rst"
  cat <<EOF
Next steps:
  1. Set your cameras + credentials, either by editing:
       $DEST/config.yaml   (cameras, location, capture settings)
       $DEST/.env          (set REOLINK_PASSWORD)
     …or from the web UI's Config tab:
       http://${ip:-<this-host>}:8080/
  2. Once configured, start capturing:
       sudo systemctl start reolapse-capture.service
  3. Set the machine timezone so capture days match local midnight:
       sudo timedatectl set-timezone America/Chicago   # your zone

The web UI and nightly build timers are already running.

Reminder: ReoLapse has no login on the video pages and ships no TLS — keep it on
your LAN, behind a VPN or reverse proxy for any remote access.
EOF
}

main() {
  trap 'err "Install failed (line $LINENO). Nothing was started; fix the error and re-run."' ERR
  preflight
  install_packages
  fetch_source
  setup_venv
  setup_config
  install_units
  install_sudoers
  enable_services
  print_next_steps
}

main "$@"
