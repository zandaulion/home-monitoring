"""Merge only Home Monitor's console entry, icon, and private proxy route.

Run with local administration rights after configuring console_admin_token in
the private runtime config. No credentials or deployment addresses enter source.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--console-source", required=True)
    parser.add_argument("--console-live", required=True)
    parser.add_argument("--caddy", default="/etc/caddy/Caddyfile")
    parser.add_argument("--port", type=int, default=8840)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("Invalid upstream port")
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text())
    token = config.get("console_admin_token", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
        raise SystemExit("Set a random console_admin_token in the private config first")
    backups = config_path.parent / "deploy-backups" / str(time.time_ns())
    backups.mkdir(parents=True, mode=0o700)
    caddy = Path(args.caddy)
    original = caddy.read_text()
    (backups / "Caddyfile").write_text(original)
    (backups / "Caddyfile").chmod(0o600)
    marker = "Home Monitor private console route"
    route = f'''\n\t# BEGIN {marker}
\thandle /home-monitor/api/admin/* {{
\t\turi strip_prefix /home-monitor
\t\treverse_proxy 127.0.0.1:{args.port} {{
\t\t\theader_up -X-Admin
\t\t\theader_up -Tailscale-User-Login
\t\t\theader_up X-Admin-Token "{token}"
\t\t}}
\t}}
\t# END {marker}
'''
    if f"# BEGIN {marker}" in original:
        updated = re.sub(r"\n\t# BEGIN " + re.escape(marker) + r".*?\n\t# END " + re.escape(marker) + r"\n", route, original, flags=re.S)
    else:
        updated, count = re.subn(r"(?m)^:80 \{", ":80 {\n"+route, original, count=1)
        if count != 1:
            raise SystemExit("Private console listener not found; no changes made")
    caddy.write_text(updated)
    result = subprocess.run(["caddy", "validate", "--config", str(caddy), "--adapter", "caddyfile"], capture_output=True, text=True)
    if result.returncode:
        caddy.write_text(original)
        raise SystemExit("Caddy validation failed; original configuration restored")
    entry = json.loads((root / "deploy/console-entry.json").read_text())
    icon = (root / "home_monitor/static/icons/icon.svg").read_bytes()
    for label, directory in (("source", Path(args.console_source)), ("live", Path(args.console_live))):
        path = directory / "apps.json"
        contents = path.read_bytes()
        (backups / f"apps-{label}.json").write_bytes(contents)
        apps = json.loads(contents)
        matches = [i for i, app in enumerate(apps) if app.get("id") == entry["id"]]
        if len(matches) > 1:
            caddy.write_text(original)
            raise SystemExit("Duplicate console entries; inspect before retrying")
        if matches:
            apps[matches[0]] = entry
        else:
            apps.append(entry)
        path.write_text(json.dumps(apps, ensure_ascii=False, indent=2)+"\n")
        icon_path = directory / entry["icon"]
        icon_path.write_bytes(icon)
        icon_path.chmod(0o644)
        owner = icon_path.parent.stat()
        if os.geteuid() == 0:
            os.chown(icon_path, owner.st_uid, owner.st_gid)
    live = Path(args.console_live)
    sw = live / "sw.js"
    old_sw = sw.read_text()
    (backups / "console-sw.js").write_text(old_sw)
    version = hashlib.sha256((live / "apps.json").read_bytes()+icon).hexdigest()[:12]
    new_sw, count = re.subn(r"const VERSION = '[^']+';", f"const VERSION = '{version}';", old_sw, count=1)
    if count != 1:
        raise SystemExit("Console worker version could not be updated")
    sw.write_text(new_sw)
    subprocess.run(["restorecon", "-R", str(live)], capture_output=True)
    result = subprocess.run(["systemctl", "reload", "caddy"], capture_output=True)
    if result.returncode:
        caddy.write_text(original)
        subprocess.run(["systemctl", "reload", "caddy"], capture_output=True)
        raise SystemExit("Caddy reload failed; previous routing restored")
    print("Home Monitor console entry and private admin route installed; other apps preserved.")


if __name__ == "__main__":
    main()
