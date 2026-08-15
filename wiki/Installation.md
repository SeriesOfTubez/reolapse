# Installation

> **Running this in a VM?** Make sure the hypervisor exposes at least the
> x86-64-v2 CPU baseline to the guest — see
> [Requirements](https://github.com/SeriesOfTubez/reolapse#requirements).
> Proxmox's default CPU type doesn't; `host` or `x86-64-v3` does.

## Docker

```bash
git clone https://github.com/SeriesOfTubez/reolapse.git
cd reolapse

cp config.example.yaml config.yaml   # edit: cameras, location, options
cp .env.example .env                 # set REOLINK_PASSWORD

docker compose pull && docker compose up -d   # runs the latest release image
```

This pulls a pre-built multi-arch image (amd64 + arm64, so it works on a
Raspberry Pi) from `ghcr.io/seriesoftubez/reolapse`. `REOLAPSE_TAG` selects
which: `latest` (newest release, default), a pinned version like `0.2.0`, or
`edge` (latest development code from `main`) — e.g.
`REOLAPSE_TAG=edge docker compose pull && docker compose up -d`. Or **build
from source** instead, handy for local changes or unreleased code:

```bash
docker compose up -d --build
```

Open <http://localhost:8080>. Three services start: `capture` (continuous),
`scheduler` (nightly/weekly builds), and `web`. Keep `storage.root: ./data` in
`config.yaml` so data lands on the Docker volume. To upgrade later, see
[Upgrading](#upgrading) below.

## Install on a Linux VM (systemd)

### Easy install (one command)

For a Debian/Ubuntu host with systemd. Installs the system packages, clones into
`/opt/reolapse`, sets up the virtualenv, installs and enables the systemd units
**for the user running the script** (retargeting the units' default `User=`, so
you don't need an `ubuntu` user), starts the web UI, and (unless you opt out)
adds the scoped sudo rule for the Config page's Restart button:

```bash
curl -fsSL https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/install.sh | bash
```

Piping a script into your shell means running it unread — reasonable to look
first (a good habit for any `curl | bash`):

```bash
curl -fsSL https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/install.sh -o install.sh
less install.sh
bash install.sh
```

By default it installs the **latest stable release**; run interactively it asks
whether you'd rather have `main` (latest development code). Run it as a normal
user with sudo — **not** as root. It deliberately stops short of capturing: you
still fill in `config.yaml` and `.env` (or use the web UI's Config tab), then
`sudo systemctl start reolapse-capture.service` — the script prints the exact
next steps.

Env-var options: `REOLAPSE_DIR=` (install location), `REOLAPSE_REF=` (a tag or
`main` to skip the prompt — e.g. `REOLAPSE_REF=main` for dev code, or
`REOLAPSE_REF=v0.1.0` to pin a release), `REOLAPSE_YES=1` (non-interactive,
takes the stable default), `REOLAPSE_SKIP_SUDOERS=1`.

### Manual install

```bash
sudo mkdir -p /opt/reolapse && sudo chown "$USER" /opt/reolapse
git clone https://github.com/SeriesOfTubez/reolapse.git /opt/reolapse
cd /opt/reolapse

sudo apt install -y ffmpeg
python3 -m venv venv && venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml   # edit for your setup
cp .env.example .env                  # set REOLINK_PASSWORD
chmod 600 .env config.yaml
```

**What you must configure** before the services will work:

1. **`config.yaml`** — your cameras (host/channel/name), and optionally location
   and capture settings. See the
   [Configuration Reference](Configuration-Reference) for every field.
2. **`.env`** — set `REOLINK_PASSWORD` (and any extra `REOLINK_PASSWORD_*`) to
   your real camera/NVR password(s). `config.yaml` only ever references these by
   name, never the literal value.

Test one capture before wiring up the services (`venv/bin/python capture.py -v` —
a JPEG should appear under `data/snapshots/…`).

**Then point the systemd units at your setup.** The units in `deploy/` are
written for the defaults **`User=ubuntu`** and **`/opt/reolapse`** — if either
differs for you they won't start, so retarget them first:

```bash
# Set User= to the account the services run as (skip if you really use "ubuntu"):
sed -i "s/^User=ubuntu$/User=$(id -un)/" deploy/*.service

# Only if you installed somewhere other than /opt/reolapse, fix the paths too:
# sed -i "s|/opt/reolapse|/your/install/path|g" deploy/*.service
```

Now install and enable them:

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reolapse-capture.service \
     reolapse-web.service reolapse-daily.timer reolapse-yearly.timer
```

Finally, make sure ReoLapse knows your timezone so capture days line up with
your local midnight: either set `capture.timezone` (an IANA name like
`America/Chicago`), leave it blank to auto-detect from your `events.zip` /
latitude-longitude, or set the host clock (`sudo timedatectl set-timezone …`).
The config option is the most reliable — it doesn't depend on the host clock
being right.

**(Optional) Enable the Config page's Restart button.** A ready-made, scoped
sudo drop-in ships in `deploy/`. It also defaults to user `ubuntu`, so set it to
your account first (and confirm the `systemctl` path matches
`command -v systemctl` — it's `/usr/bin/systemctl` on most systems), then
validate and install it:

```bash
sed -i "s/^ubuntu /$(id -un) /" deploy/reolapse.sudoers   # set your user

sudo visudo -cf deploy/reolapse.sudoers \
  && sudo install -m 0440 -o root -g root deploy/reolapse.sudoers /etc/sudoers.d/reolapse
```

## Upgrading

Upgrades only change code — your `config.yaml`, `.env`, and `data/` are
gitignored (Linux) or on a named volume (Docker), so history and settings are
never touched.

**Linux (installed with `install.sh`):**

```bash
curl -fsSL https://raw.githubusercontent.com/SeriesOfTubez/reolapse/main/upgrade.sh | bash
```

…or `bash upgrade.sh` from your install dir. It fetches the latest release,
reinstalls dependencies, refreshes the systemd units, and restarts the
services. Pin a version with `REOLAPSE_REF=v0.2.0`, or track the tip of `main`
with `REOLAPSE_REF=main`.

**Docker:**

```bash
cd /path/to/reolapse
git pull                                # refresh compose file + docs
docker compose pull && docker compose up -d   # or: up -d --build to build from source
```

The `data` volume survives across both. (`git pull` just updates the compose
file; the actual app comes from the pulled image or a local build.)

Releases are tagged and listed on the
[Releases page](https://github.com/SeriesOfTubez/reolapse/releases). The running
version appears in the web UI header and in the capture/web service logs.
