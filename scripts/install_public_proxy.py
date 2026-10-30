"""Install a public loopback boundary; DNS and tunnel routes are separate.

Deployment addresses are arguments, never tracked source material.
"""
import argparse
from pathlib import Path
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--upstream-port", type=int, default=8840)
    parser.add_argument("--caddy", default="/etc/caddy/Caddyfile")
    parser.add_argument("--backup-dir", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", args.hostname) or any(not 1 <= p <= 65535 for p in [args.port, args.upstream_port]):
        raise SystemExit("Invalid hostname or port")
    path = Path(args.caddy)
    original = path.read_text()
    marker = "Home Monitor public loopback proxy"
    if f"# BEGIN {marker}" not in original and re.search(r"(?m)^:"+str(args.port)+r"\s*\{", original):
        raise SystemExit("The requested listener is already in use")
    # Deliberately no access log: neither request identity nor camera URLs
    # belong in a public proxy's diagnostic files.
    block = f'''# BEGIN {marker}
:{args.port} {{
    bind 127.0.0.1
    encode zstd gzip
    header {{
        -Server
        Cache-Control "no-store"
        CDN-Cache-Control "no-store"
        Cloudflare-CDN-Cache-Control "no-store"
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
    }}
    request_body {{
        max_size 9MB
    }}
    @private_api path /api/admin /api/admin/* /api/ingest /api/ingest/*
    respond @private_api 404
    reverse_proxy 127.0.0.1:{args.upstream_port} {{
        header_up Host {args.hostname}
        header_up -Authorization
        header_up -X-Admin
        header_up -X-Admin-Token
        header_up -Tailscale-User-Login
        header_up -Tailscale-User-Name
        header_up -Tailscale-User-Profile-Pic
    }}
}}
# END {marker}
'''
    if f"# BEGIN {marker}" in original:
        updated = re.sub(r"# BEGIN "+re.escape(marker)+r".*?# END "+re.escape(marker)+r"\n?", block, original, flags=re.S)
    else:
        updated = original.rstrip()+"\n\n"+block
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / f"Caddyfile-public-{time.time_ns()}"
    backup.write_text(original)
    backup.chmod(0o600)
    path.write_text(updated)
    checked = subprocess.run(["caddy", "validate", "--config", str(path), "--adapter", "caddyfile"], capture_output=True)
    if checked.returncode:
        path.write_text(original)
        raise SystemExit("Caddy validation failed; original configuration restored")
    result = subprocess.run(["systemctl", "reload", "caddy"], capture_output=True)
    if result.returncode:
        path.write_text(original)
        subprocess.run(["systemctl", "reload", "caddy"], capture_output=True)
        raise SystemExit("Caddy reload failed; original configuration restored")
    print("Public proxy installed: private routes blocked, identity headers stripped, CDN caching disabled.")


if __name__ == "__main__":
    main()
