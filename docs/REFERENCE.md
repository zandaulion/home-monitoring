# Home Monitor

A private, snapshot-based person and face recognition system. An optional
[recorder](../deploy/README.md#optional-event-video) buffers video in memory near
the cameras and uploads a clip of each event to the monitoring server.
Camera SD recordings remain independent.

## Components

- `collector.py`: runs near the cameras. Samples their authenticated ONVIF
  snapshot URLs every two seconds (every second during movement), compares small grayscale images using Pillow,
  and uploads frames during movement plus a periodic frame every 15 seconds.
  Frames are kept in RAM on the collector. After movement, the faster sampling
  helps capture multiple views of a face.
- `home_monitor`: FastAPI service, SQLite event history, bounded snapshot storage,
  and a responsive dashboard. YOLOX-Nano detects people, YuNet detects faces, and
  SFace produces normalised face embeddings. All inference runs on the server.
- Cloudflare Tunnel and Caddy: public HTTPS, with device activation required for
  camera data. Public routes strip private credentials and deny admin/collector APIs.
- Tailscale Serve: private owner access, the invitation console, and collection.
  The collector uses a separate token that cannot read images or dashboard APIs.

## Initial setup

For a complete two-machine installation, service templates, and the collector's
cron fallback, follow [deploy/README.md](../deploy/README.md). All tracked examples
use synthetic values; keep the real configuration and data outside Git.

Python 3.12 is recommended. Install `requirements.txt` into a virtual environment,
then run `python scripts/download_models.py`. Downloads are checked against
pinned SHA-256 hashes. Model sources and hashes are saved in the runtime manifest.

Create `.runtime/config.json` with permissions `0600`, inside a `0700` runtime
directory. The following values are deliberately synthetic:

```json
{
  "data_dir": "/srv/home-monitor/private/data",
  "model_dir": "/srv/home-monitor/private/models",
  "owner_login": "owner@example.invalid",
  "collector_token": "REPLACE_WITH_A_RANDOM_SECRET",
  "admin_token": "REPLACE_WITH_A_DIFFERENT_RANDOM_SECRET",
  "public_origin": "https://monitor.example.invalid",
  "public_base_url": "https://monitor.example.invalid",
  "allowed_origins": ["https://monitor.example.invalid", "https://private.example.invalid"],
  "sampling_seconds": 2,
  "cameras": [{"id": "camera-1", "name": "Entrance"}]
}
```

Set `HOME_MONITOR_CONFIG` to that file and launch:

```sh
python -m uvicorn home_monitor.app:create_app --factory \
  --host 127.0.0.1 --port 8840 --no-proxy-headers --no-access-log
```

Bind only to loopback. Tailscale Serve provides private owner/collector access;
a separate hardened Caddy listener serves the public tunnel. `--no-proxy-headers` is
required: the application trusts identity headers only from its loopback proxy.
Do not expose this server through another proxy that accepts caller-supplied
Tailscale identity headers. The optional admin token is for local diagnostics;
never embed it in browser source or URLs.

## PWA and invitation console

The public HTTPS address is an installable PWA. Its manifest includes
normal and maskable icons, standalone display, and an iOS home-screen icon.
The in-app Install button opens the native install prompt where supported and
otherwise shows platform instructions. Activated devices use the public app
without Tailscale. Administration and the collector connection remain private.

For public deployment, set `public_origin` and `public_base_url` to the same
public HTTPS origin, retaining the private console origins in `allowed_origins`.
The application disables owner-header and diagnostic-token bypasses on the
public hostname, and denies its admin/collector routes regardless of credentials.
Run `scripts/install_public_proxy.py` with the hostname and a backup directory
to add the public loopback listener. On SELinux hosts, label its selected port as
`http_port_t` first. The installer validates Caddy and restores prior routing if
validation/reload fails. It enables no access log, strips identity/authorization
headers, pins the upstream Host, and sends browser/CDN no-store headers.

Add one hostname rule on a Cloudflare Tunnel pointing at that listener,
before the catch-all rule,
then create a proxied CNAME to the same tunnel subdomain. Preserve every existing
ingress rule and tunnel setting. Do not route the public tunnel straight to the
application or private console listener. Use live DNS/ingress reads to check the
result, and keep account/tunnel IDs and routing backups in ignored runtime storage.

The private owner URL remains available. Cookies, PWA installations, and push
subscriptions are origin-specific: moving from the private address to the public
address requires a new invitation, installation from the public origin, and
notification opt-in there. Face enrolments and server history remain shared.

The update mechanism comes from the sibling `pwa-kit`: `pwa-update.js` and
`sw-update.js` are vendored, and the reference network-first worker is adapted
to an explicit interface-file allowlist. API responses, camera snapshots, face
crops, uploads, invitation codes, and event data never enter Cache Storage.
The worker version is a hash of all static file contents, computed at server
startup. Restart the server after a deployment. `/bust` is the kit's cache and
worker reset escape hatch, externalised for the strict content security policy.

Two local safety adaptations preserve unfinished forms while a phone is locked,
and prevent the grace fallback from navigating a client that already reloaded.
Updates wait for photo uploads, open dialogs, and unsaved settings. An offline
launch shows an empty interface and connection guidance; it does not retain
private images for offline viewing. Web Push is separately enabled in Settings.

`deploy/console-entry.json` is the source of truth for this app's invitation
console entry. Configure a separate random `console_admin_token` in the private
server configuration, `public_base_url` as the public PWA origin, and include
the private console origin in `allowed_origins`. Run `scripts/install_console.py`
with the config and the console source/live directories to merge the entry,
install the app icon, and add the private Caddy route. It preserves other apps,
validates the changed Caddy configuration, and keeps backups in ignored runtime
storage. No secret is copied into JavaScript or console configuration.

The console supports creating/cancelling seven-day invitations and listing,
renaming, revoking, restoring, and deleting devices. Its API requires a separate
secret injected by the private listener. Neither the owner identity, the capture
token, nor an activated device grants console API access. Invitations grant
access to the whole monitoring app, including face-library editing; there are
no guest/read-only roles in this version.

An invitation is activated only on explicit submission, not by opening its link.
The code is in a URL fragment to avoid server access logs. On first activation,
the plaintext code is erased and an HttpOnly, Secure, SameSite=Strict, host-only
cookie is issued; only its hash is stored. For one hour after first use, the code
can rotate access onto the same device record so it can be moved into an installed
PWA. Rotation invalidates the previous browser's token. Expired, cancelled, or
revoked access cannot be revived by reusing the code. The tailnet owner can still
access the private owner URL without a device cookie; the public URL always
requires an activated device. Opening the app renews a device's access; a device
unused for 400 days expires and needs a new invitation. An invalid/revoked cookie fails closed instead of
falling back to the private owner bypass.

The collector needs only Pillow and a private config:

```json
{
  "server_url": "https://private.example.invalid",
  "collector_token": "SAME_RANDOM_COLLECTOR_SECRET",
  "interval_seconds": 2,
  "motion_fraction": 0.015,
  "cameras": [{
    "id": "camera-1",
    "snapshot_url": "http://192.0.2.10/snapshot.jpg",
    "username": "EXAMPLE_USER",
    "password": "EXAMPLE_PASSWORD"
  }]
}
```

Optional keys: `active_interval_seconds` sets sampling during movement (default 1),
and `presence` enables automatic Away with synthetic values such as
`{"subnet": "192.0.2.0/24", "macs": ["02:00:00:00:00:01"]}`. Sleeping phones may
answer late, so `grace_seconds` (default 900) sets how long a phone must be silent
before it counts as gone. Addresses stay in this file and never reach the server.

Run `python collector.py --config /path/to/private-config.json`. Install as a
user service with automatic restart. Where user lingering is unavailable, use
a user cron boot/periodic watchdog and a shared `flock` lock to prevent duplicate
collectors. Capture errors use bounded retries; logs never print credentials.

## Using recognition

1. Open **People**, add a name, and upload several clear portraits with one face.
2. Review actual camera events. Poor or small faces remain unidentified.
3. **Correct label** changes an event. **Label & learn** also adds a clear face
   to that person's library. Do not train on incorrect or uncertain labels.
4. Remove unsuitable samples or delete a person to remove their enrolled samples
   and identity associations. Camera recordings are managed separately in your camera app.

Names require two matching observations within 20 seconds. Matching also requires
a minimum similarity and separation from the next-best person. Similarity is not
a probability. This does not provide liveness checks, guaranteed identification,
or persistent identity tracking between cameras. Unknown faces and unavailable
faces are distinct states. Small or blurred faces are never used for enrolment.

Web Push alerts are optional and enabled per device in Settings. They can arrive
with the app closed. On iPhone/iPad, enable them from the installed Home Screen
app. The browser/OS must permit notifications and remain connected to its push
service; Focus, battery restrictions, force-stopping the browser, and offline
devices may delay or prevent delivery. There is no email or SMS integration.

The public app supports subscription and activity review without Tailscale.
Incoming push messages
are encrypted and displayed directly by the cached worker without an origin
request. Payloads contain a generic title and message, with no camera image,
person name, or face embedding. Browser push relays still handle delivery.

When enabling notifications as the tailnet owner, the app registers an owner
device cookie so it can be managed and revoked in the console. Each device has
at most one subscription. Revoking or deleting it removes its subscription and
pending jobs; transferring an invitation to another browser removes the prior
browser's subscription. Restoring access requires enabling notifications again.
Already accepted push messages cannot be recalled from the relay.

The sender's VAPID private key is generated once at
`.runtime/data/vapid-private.pem` (mode 0600); preserve it with the private
database when backing up. Regenerating it invalidates browser subscriptions.
Subscription endpoints and their keys are private and never enter API responses,
source control, the PWA cache, or application logs. Only recognised browser push
service hosts on HTTPS port 443 are accepted as delivery endpoints.

**Person-only mode.** Settings can turn face recognition off for cameras that
cannot resolve faces. Faces are then neither detected nor stored, events have a
single `person` state. A confident detection alerts on the first observation
because a fast intruder may only appear once. A weak one, such as a tree at night,
needs a second observation in a different place within eight seconds. Weak
detections are recorded either way. Enrolled samples are kept unused;
remove them from the face library if they are no longer wanted.

**Vehicles and animals.** The person model also reports cars, trucks, buses,
motorcycles, bicycles, cats, dogs, horses, sheep and cows. They are recorded as
Activity when they appear in a new position, so a parked car stays quiet, and
anything visible when a camera first reports after a restart is treated as
scenery. Something that stays for five minutes is remembered for two days, so
darkness cannot turn it into a new arrival, and a new position must be seen twice
within eight seconds. A vehicle or animal seen in only one frame is not recorded. Their alerts are off by default and are separate settings. People always
take priority: a frame with a person is a person event.

**Alerts and quiet detections.** Each event records why it did or did not alert.
Confident or moving detections are alerts, even when the cooldown or a snooze
silenced their notification, and the overview lists the last ten. Weak, lone or
stationary detections are quiet: the overview counts them per camera for the last
24 hours, and Activity can filter by either. Quiet events are kept for two days at
most and are removed first when storage runs short, so they cannot push out alerts.

**When alerts fire.** Person alerts can be always on, limited to a daily schedule
in the time zone of the device that saved it, or off. A schedule can cross
midnight. Away mode turns alerts on regardless, from the app's Away button or
automatically when no listed phone has been on the home network for 15 minutes.
The presence report is a single yes or no; a report older than five minutes is
ignored. Snooze still applies on top of these. Recognised activity never alerts.

**Camera offline alerts.** Every device is told once when a camera sends no
snapshot for five minutes, and once when it returns. If all cameras stop, one
message covers them. These alerts name the camera, ignore snooze, and stop while
monitoring is paused. They come from the server, so a stopped server cannot
report itself.

**Tapping an alert** opens that event. Person alerts carry its opaque identifier.

With recognition on, unidentified-person alerts include unrecognised faces, missing or unclear faces,
and groups where not everyone is identified. They require two person observations
on the same camera less than 20 seconds apart, with no intervening empty or fully
recognised frame. An enrolled face library is not required; fully recognised
activity does not alert. They are limited to one per camera per cooldown interval (five minutes by
default, configurable from 1–60 minutes in Settings) and one per event per device.
The alert banner can snooze alerts on all devices for 30 minutes to eight hours,
or resume them early. Snooze expires automatically and cancels queued activity
alerts; capture and activity history continue, and test notifications remain available.
Snooze does not recall messages already accepted by a push relay.

Activity has a graphical timeline with one lane per camera and an hourly
histogram. It opens on the last 24 hours, which follows the current time, and a
date picker reaches any single day still retained. On phones, views wider than two hours
show one row per camera with one block per hour, darker for more events; tapping a
block lists its events. Zoom to six hours, two hours, or thirty
minutes, then use the focus slider or Earlier/Later controls to move through the
day. Clicking the histogram focuses a two-hour period. The full day's compact
metadata loads independently of snapshot pagination, including events crossing
midnight. Days with daylight-saving changes can contain 23 or 25 hours.

Click a camera period to review its snapshots, twelve at a time. Live updates
keep the selected snapshot list stable; click the period again or Refresh activity
to include newly recorded events. Events on the
same camera are grouped across gaps of up to ten minutes; individual event spans
remain separate coloured segments so gaps stay visible. Recognition colours and
filters do not imply identity tracking between cameras. The hourly histogram
counts event starts (events carried over from the previous day count in the
first hour), rather than individual model observations.

Snoozed intervals are shown as background bands. Camera status history and push
relay acceptance times are recorded from this version onward; earlier coverage
is unknown. A healthy collector heartbeat provides up to 100 seconds of status
coverage. Camera problems, paused monitoring, and unknown coverage have distinct
backgrounds. Healthy heartbeats are collector reports, not continuous video or
proof that every person was detected. Alert diamonds mark the first recorded
push-service acceptance per event across devices, not confirmation that a phone
displayed a notification. These histories follow activity retention limits.

A SQLite outbox survives restarts, retries transient failures up to three
times, and expires alerts after 15 minutes. Cancelled/corrected/deleted events
and paused monitoring stop pending activity alerts. Missing/expired subscriptions
are removed on 404/410. The test button targets only the current device and is
limited to one request per 30 seconds. Server acceptance is not proof that an OS
displayed a notification; use a test on the actual phone to verify permission and
background delivery.

## Retention and privacy

Each event keeps up to five photos at least two seconds apart, replacing the
weakest with better frames; the largest, sharpest view is shown first.

Defaults: seven days and 2 GB for event images. A minute-by-minute cleanup removes
the oldest events when either bound is reached, including corresponding metadata
and face crops. Latest snapshots are overwritten; stale ones expire after an
hour. Enrolled faces are kept until explicitly removed (maximum 100 people,
50 samples each). Pausing removes latest snapshots and stops new recognition;
existing event history is retained according to the configured limits.

Camera times are not trusted for the timeline; event times use server receipt
time and display in the viewer's timezone. No third-party fonts, analytics,
inference services, or image hosting are used.

Runtime configuration, model weights, face data, images, databases, and QA images
are excluded from version control. Never commit or publish real camera addresses,
identities, credentials, deployment metadata, or images. The runtime directory is
not an application asset directory.

## Operations

On the server, use `systemctl --user status home-monitor` and
`journalctl --user -u home-monitor`. On the collector, the service is
`home-monitor-collector`; cron fallback logs use the `home-monitor-collector`
syslog tag. Persistent user services/watchdogs restart after failure and boot.
Do not run multiple application workers: inference and SQLite bookkeeping are
designed for a single process.

Settings can pause monitoring, rename cameras, adjust retention, and tighten
matching thresholds. Camera passwords are only in the collector's private config;
if changed in your camera app, update that config and restart the collector. Keep the
camera and server software maintained. There is no automated backup; camera SD
recordings remain independent if the internet or recognition server is unavailable.

## Validation

Install `requirements-dev.txt` and run `python -m pytest -q`. Tests use synthetic
fixtures for authentication, cross-origin protection, enrolment, label corrections,
retention, storage limits, and the difference between a person and an unknown face.
Run `node tests/activity-timeline.mjs` and `node tests/push-worker.mjs` for the
timeline calculations and worker behaviour. With Playwright Chromium installed,
run `python tests/timeline-browser.py` for desktop/mobile interaction checks using
a temporary local server and synthetic events. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE`
if Chromium is installed separately. Synthetic screenshots stay in `.runtime/qa/`.

Use public upstream images in temporary storage for real-model checks; do not
enrol household members as test identities or add private fixtures to source.

## Model references

- [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX): Apache-2.0.
- [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet): MIT; see upstream model license.
- [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface): Apache-2.0; see upstream model license.

The application is designed to run on ARM64 CPUs without a GPU. Actual accuracy
depends on camera placement, face size, lighting, and the enrolled examples.
