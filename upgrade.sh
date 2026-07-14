#!/usr/bin/env bash
#
# ReoLapse upgrader for a git-based Linux install (i.e. one made with install.sh).
#
#   curl -fsSL https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/upgrade.sh | bash
#
# or, from your install dir:  bash upgrade.sh
#
# Upgrades the code to the latest release, reinstalls dependencies, refreshes
# the systemd units, and restarts the services. Your data, config.yaml, and
# .env are gitignored, so they're never touched.
#
# Env-var options:
#   REOLAPSE_DIR=/opt/reolapse   install location
#   REOLAPSE_REF=v0.2.0          upgrade to a specific tag/branch instead of the
#                                latest release (e.g. REOLAPSE_REF=main for tip)
#
# All logic is in functions called from main() at the very end, so a truncated
# download can't run a half-command.

set -euo pipefail

DEST="${REOLAPSE_DIR:-/opt/reolapse}"

if [ -t 1 ]; then
  c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
else
  c_bold=""; c_grn=""; c_ylw=""; c_red=""; c_rst=""
fi
info() { printf '%s==>%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s!!  %s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%sxx  %s%s\n' "$c_red" "$*" "$c_rst" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

preflight() {
  [ "$(id -u)" -ne 0 ] || { err "Run as a normal user with sudo — not as root."; exit 1; }
  have sudo || { err "sudo is required."; exit 1; }
  have git  || { err "git is required."; exit 1; }
  [ -d /run/systemd/system ] || { err "systemd not detected. For Docker, see the README's upgrade steps."; exit 1; }
  if [ ! -d "$DEST/.git" ]; then
    err "$DEST is not a git checkout — upgrade.sh only works for installs made with install.sh."
    err "(If you deploy some other way, update your files however you normally do.)"
    exit 1
  fi
}

pick_target() {
  git -C "$DEST" fetch --tags --force --prune origin >/dev/null 2>&1
  if [ -n "${REOLAPSE_REF:-}" ]; then
    echo "$REOLAPSE_REF"; return
  fi
  local latest_tag
  latest_tag="$(git -C "$DEST" tag -l 'v*' | sort -V | tail -1)"
  if [ -n "$latest_tag" ]; then
    echo "$latest_tag"
  else
    local br; br="$(git -C "$DEST" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
    [ "$br" = "HEAD" ] && br="main"
    echo "origin/$br"
  fi
}

main() {
  trap 'err "Upgrade failed (line $LINENO). Your install was not changed beyond what already ran."' ERR
  preflight

  local before target
  before="$(cat "$DEST/VERSION" 2>/dev/null || echo unknown)"
  target="$(pick_target)"
  info "Current version: $before  ->  upgrading to: $target"

  info "Fetching and checking out $target…"
  git -C "$DEST" reset --hard "$target"

  info "Reinstalling Python dependencies…"
  "$DEST/venv/bin/pip" install --quiet --upgrade pip
  "$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"

  info "Refreshing systemd units (User=$RUN_USER, dir=$DEST)…"
  local tmp; tmp="$(mktemp -d)"
  for f in "$DEST"/deploy/reolapse-*.service "$DEST"/deploy/reolapse-*.timer; do
    sed -e "s|^User=ubuntu$|User=$RUN_USER|" -e "s|/opt/reolapse|$DEST|g" \
        "$f" > "$tmp/$(basename "$f")"
  done
  sudo cp "$tmp"/reolapse-*.service "$tmp"/reolapse-*.timer /etc/systemd/system/
  rm -rf "$tmp"
  sudo systemctl daemon-reload

  info "Restarting services…"
  sudo systemctl restart reolapse-capture.service reolapse-web.service

  local after; after="$(cat "$DEST/VERSION" 2>/dev/null || echo unknown)"
  printf '\n%sUpgraded: %s -> %s%s\n' "$c_bold" "$before" "$after" "$c_rst"
  echo "Your config.yaml, .env, and data were left untouched."
}

main "$@"
