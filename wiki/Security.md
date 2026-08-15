# Security

- **There is no authentication.** The web UI has no login, no access control,
  nothing — anyone who can reach port 8080 can browse and download every
  video. **ReoLapse is a private, LAN-only tool. Do not port-forward it or
  otherwise expose it to the internet.** If you need remote access, put it on
  a VPN (Tailscale, WireGuard) or behind a reverse proxy that adds its own
  auth and TLS — don't rely on ReoLapse itself for either. This was a
  deliberate design choice, not an oversight: ReoLapse was built to run on
  your home network, so authentication was not a priority for MVP release.
  It may get added later if there's enough demand — open an issue if that's
  you.
- **The Config page can write `config.yaml` and scan your network — the
  no-auth warning above applies doubly to it.** Anyone who can reach the app
  can reconfigure your cameras or trigger a network scan; on an open install
  the LAN-only posture is what protects this, not anything in the app itself.
  The write endpoint does reject literal passwords (only `${VAR}` references
  are accepted) and validates structure before touching the file, and its
  `Content-Type: application/json` requirement means a plain cross-origin
  `<form>` POST from some other site can't trigger it — but a malicious page
  making a same-network JSON `fetch()` still could, same as it could hit any
  other unauthenticated route here.
- **Optional Config-page passcode.** You can put a single passcode (no
  username) in front of the Config page and its write/scan endpoints
  (`/api/config`, `/api/discover`, `/api/discover/identify`, `/api/restart`,
  `DELETE /api/videos/...`) while video browsing stays open for casual LAN
  viewing. It's **opt-in**:
  set it from the Config page itself under **Config page access** (or remove
  it there later). Details:
  - The passcode is stored only as a **salted scrypt hash** in `config.yaml`
    (`webapp.config_passcode_hash`) — never in the clear, and never sent to
    the browser. A hash is safe to keep there because, unlike a camera
    password, it never needs to be reversed.
  - A successful login sets an `HttpOnly`, `SameSite=Lax` session cookie
    (server-side token, 12-hour expiry). `SameSite=Lax` keeps the cookie off
    cross-site POST/`fetch`, so a malicious off-origin page can't ride your
    session to the write endpoints. Restarting the web service or changing
    the passcode logs every session out.
  - There's a modest failed-login throttle. This is a convenience gate for a
    trusted LAN, **not** a substitute for the VPN/reverse-proxy guidance
    above — the cookie rides over plain HTTP since ReoLapse serves no TLS.
- **Deleting a video is gated the same way.** With no passcode set, anyone
  on the LAN can delete a video from the player — but that's the same
  exposure as being able to rewrite `config.yaml` and restart services,
  which they already have on an open install.
- Credentials live in `.env` (gitignored), never in `config.yaml`.
- Prefer a **dedicated, least-privilege** camera/NVR account for ReoLapse. The
  Snap API passes credentials as URL parameters, so avoid `&`, `#`, `%` in that
  password.
- The bundled Flask server is a dev-grade WSGI server — fine for a trusted
  LAN, not a hardened production server.
