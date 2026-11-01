# Deploying the server and collector

The same source checkout supports both roles. The server runs recognition and
the dashboard; the collector runs on a Linux machine that can reach the cameras.
No existing machine addresses, accounts, credentials, images, or data are needed
to reuse the code. Configure those privately on the new machines.

## Server

Install Python 3.12, venv support, Tailscale, and (for public access) Caddy and
cloudflared. Clone this repository to a directory owned by the service user.
The examples below assume `/srv/home-monitoring`; substitute your checkout path.
`manage.py init` generates random server tokens and private config/service files
without overwriting existing config. Template values still require local editing.
For an existing deployment, keep its private config and run `doctor` with
`--config /path/to/its/config.json`; do not rerun `init`.

```sh
cd /srv/home-monitoring
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
umask 077
mkdir -p .runtime
chmod 700 .runtime
.venv/bin/python scripts/manage.py init server
.venv/bin/python scripts/download_models.py
```

Edit `.runtime/config.json` locally. Set `model_dir` to the absolute path of the
downloaded `.runtime/models` directory and `data_dir` to a private writable
directory. Use the same camera IDs on both machines. Replace every secret
placeholder with a different random value; the collector token alone is shared
with the collector. Generate each value locally with:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(40))'
```

Set `owner_login` to your authenticated Tailscale login. Set `allowed_origins`
to exactly the HTTPS origins used by your dashboard and optional console.
For private-only use, remove `public_origin`, set `public_base_url` to the private
HTTPS origin, and omit public DNS, Caddy's public listener, and the tunnel.

Run `.venv/bin/python scripts/manage.py doctor server` after editing the config.
The check verifies private permissions, placeholders, model hashes, and origin
consistency without printing secret values. Then install the generated user
service, which already contains your absolute checkout path:

```sh
mkdir -p ~/.config/systemd/user
cp .runtime/home-monitor.service ~/.config/systemd/user/home-monitor.service
systemctl --user daemon-reload
systemctl --user enable --now home-monitor.service
```

An administrator can enable lingering for this user to start the user service
after reboot without an interactive login. The service uses a single worker and
binds only to loopback; keep `--no-proxy-headers` and `--no-access-log`.

Configure Tailscale Serve to reverse proxy your private HTTPS origin to
`http://127.0.0.1:8840`. Restrict the tailnet access policy to the owner and the
collector as appropriate. Confirm owner access works, an untrusted identity
cannot read `/api/status`, and the collector token cannot read dashboard data.

## Collector

Clone the repository on a machine with camera-network access. Only Pillow is
required; do not install the server's inference dependencies there.

```sh
cd /srv/home-monitoring
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-collector.txt
umask 077
mkdir -p .runtime
chmod 700 .runtime
.venv/bin/python scripts/manage.py init collector
```

Edit that private file: set the server's private Tailscale HTTPS URL, the shared
collector token, camera IDs, authenticated snapshot URLs, and camera credentials.
The documented `192.0.2.10` address is reserved for examples and must be replaced.
Check each camera's snapshot format in its documentation. Continuous video stays
on camera storage; this collector sends selected snapshots from memory.

```sh
mkdir -p ~/.config/systemd/user
cp .runtime/home-monitor-collector.service ~/.config/systemd/user/home-monitor-collector.service
systemctl --user daemon-reload
systemctl --user enable --now home-monitor-collector.service
```

Run `.venv/bin/python scripts/manage.py doctor collector` after editing the
private file. It validates structure and permissions, not live camera or server
connectivity. The generated service already uses your checkout path. As on the server,
enable user lingering if available. Where it is unavailable, use `crontab -e`
to append these lines, substituting the checkout path and preserving other jobs:

```cron
@reboot /bin/sh /srv/home-monitoring/deploy/collector-watchdog.sh
* * * * * /bin/sh /srv/home-monitoring/deploy/collector-watchdog.sh
```

The service and watchdog share `.runtime/collector.lock` through `flock`, so
they cannot capture simultaneously. Keep that lock path consistent if adapting
the templates. The watchdog logs generic collector messages to syslog.

## Optional public PWA and invitation console

Use the public-proxy installer described in [docs/REFERENCE.md](../docs/REFERENCE.md#pwa-and-invitation-console), then point a tunnel
hostname at its loopback listener. Keep tunnel credentials and DNS/account
identifiers in private system configuration or ignored runtime storage. The
public hostname must match `public_origin` and `public_base_url` in the server
configuration. Never tunnel directly to the application: the public proxy strips
identity headers and blocks collector and administration routes.

The separate invitation-console application is optional and is not bundled here.
If you already run a compatible console with an `apps.json` list and private
Caddy `:80` listener, `install_console.py` merges this app's entry into it. It
requires the console's source/live paths and a private `console_admin_token`.
Without that console, invitations can be managed through the server's existing
`/api/admin/invites` and `/api/admin/devices` APIs from a trusted local connection
using the `X-Admin-Token` header. Keep the token out of shell history and logs.

## Verification, upgrades, and private backups

Verify both services after a fresh install and after reboot. Confirm a camera
heartbeat and snapshot arrive, an unauthorized device cannot read them, and an
activated device can. If public access is enabled, confirm `/api/admin/devices`
and `/api/ingest/camera-1/heartbeat` are denied on that hostname even with valid
credentials. Test recognition with consenting subjects locally; never add their
images to Git. Verify background notifications on an actual device.

For upgrades, stop the relevant service (and temporarily disable collector cron
jobs, if used), update the checkout and dependencies, then start it again.
Restart the server to update its PWA asset version. Keep private runtime paths
intact. With `--no-access-log`, service status and generic errors are available
through `journalctl --user -u home-monitor` or `-u home-monitor-collector`.

Git stores the code and synthetic templates only. For disaster recovery, make
separate encrypted backups of both private configuration files and the server's
data directory. Stop the server while copying SQLite data, or use SQLite's backup
API. Preserve `vapid-private.pem` with the database to retain push subscriptions.
Model weights can be downloaded again with hash verification. Recreate machine
authentication, DNS, and tunnel configuration separately. A fresh deployment
starts with an empty event history and face library unless you restore a private
backup. Never upload these backups or machine-specific configuration to GitHub.

## Optional event video

`recorder.py` runs alongside the collector and requires FFmpeg with RTSP support.
It keeps a rolling eleven minutes of each stream in memory, then turns each event
into a clip. Recording never writes to the USB drive, so a slow or missing drive
cannot interrupt it. Allow about 200 MB of memory for three cameras.

Add `rtsp_url` to each camera in the private collector configuration; use the
camera's ONVIF stream URI and the existing username/password fields. Add:

```json
"recording": {
  "mount": "/mnt/recordings",
  "buffer": "/tmp/home-monitor",
  "ffmpeg": "/usr/bin/ffmpeg",
  "retention_hours": 24,
  "reserve_mb": 3072
}
```

The buffer must be on tmpfs, and the recorder refuses to start otherwise. Avoid
`/dev/shm` unless the user has lingering enabled: systemd removes a user's files
there when their last session ends. `mount` is
the optional USB drive that keeps a copy of each clip for seven days; without it,
clips are stored on the server only.

Run `python3 recorder.py --config .runtime/collector-config.json` under a service
or boot watchdog, using `flock` to prevent duplicate instances. Keep a private
umask of `0077`. FFmpeg errors are suppressed because they can contain credentials.
Logs contain generic camera IDs and failure types only. Record video without audio.

Every event gets a clip, whether or not it sent an alert: from 20 seconds before
the event until 20 seconds after it ends, up to five minutes. A visit still going
after 45 seconds sends a first clip early, and the finished clip replaces it.
Footage with gaps is attached and marked as incomplete instead of being discarded.
Clips cover events from the last ten minutes, so an outage of the server or network
longer than that loses clips. Memory is lost in a power cut, which is why clips
leave the machine quickly. Cameras retain their own SD recordings.

The recorder reports to the server every few seconds. If it stops reporting, or a
camera's footage stops arriving, for five minutes, every device is told once, and
again when recording returns. Settings shows the current state. These alerts use
the camera alerts setting, ignore snooze, and stop while monitoring is paused. A
camera that is already offline has its own alert instead.

A private playback copy is uploaded to the server and appears in the event
window. It uses the same device authentication and retention/storage limits as
snapshots, supports seeking, and is excluded from offline caching. USB clips
expire after seven days or earlier under space pressure; server event deletion
removes the server copy only. Monitoring pause also pauses recording when the
recorder next reaches the server.
