"""Create private deployment files and diagnose a fresh installation without exposing secrets."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def initialize(role, root=ROOT):
    root = Path(root).resolve()
    private = root / '.runtime'
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    name = 'config.json' if role == 'server' else 'collector-config.json'
    target = private / name
    template = json.loads((ROOT / 'deploy' / f'{role}-config.example.json').read_text())
    if role == 'server':
        template.update(data_dir=str(private / 'data'), model_dir=str(private / 'models'))
        for key in ('collector_token', 'admin_token', 'console_admin_token'):
            template[key] = secrets.token_urlsafe(40)
    else:
        template['collector_token'] = 'COPY_THE_SERVER_COLLECTOR_TOKEN_HERE'
    # Exclusive creation: never overwrite a working installation's configuration.
    try:
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
            file.write(json.dumps(template, indent=2)+'\n')
    except FileExistsError:
        raise ValueError('Private configuration already exists; no configuration was overwritten')
    service = 'home-monitor' if role == 'server' else 'home-monitor-collector'
    body = (ROOT / 'deploy' / f'{service}.service.example').read_text().replace('/srv/home-monitoring', str(root))
    # The generated file is private and intentionally not installed automatically.
    (private / f'{service}.service').write_text(body)
    (private / f'{service}.service').chmod(0o600)
    print('Created private configuration and a service file under .runtime/.')
    print('Edit the synthetic addresses, account and camera entries before running doctor.')
    if role == 'collector':
        print('Copy the collector token from your private server config; it was not generated here.')


def diagnose(role, config_path):
    issues = []
    path = Path(config_path)
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return ['Configuration cannot be read as JSON']
    if not isinstance(config,dict):
        return ['Configuration must be a JSON object']
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        issues.append('Configuration permissions must be 0600')
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        issues.append('Configuration directory permissions must be 0700')
    required = ('owner_login', 'collector_token', 'data_dir', 'model_dir') if role == 'server' else ('server_url', 'collector_token')
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip():
            issues.append('Missing field: '+key)
    if any(not isinstance(config.get(key),str) or not config[key].strip() for key in required):
        return issues
    def inspect(value, key=''):
        if isinstance(value, dict):
            for k,v in value.items(): inspect(v,k)
        elif isinstance(value,list):
            for v in value: inspect(v,key)
        elif isinstance(value,str) and any(marker in value for marker in ('.invalid','192.0.2.','REPLACE_','COPY_THE_','EXAMPLE_','SAME_RANDOM_')):
            issues.append('Replace the synthetic value for: '+key)
    inspect(config)
    cameras = config.get('cameras')
    if not isinstance(cameras,list) or not cameras:
        issues.append('Configure at least one camera')
    else:
        import re
        ids = [c.get('id') for c in cameras if isinstance(c,dict)]
        if len(ids) != len(cameras) or len(set(str(v) for v in ids)) != len(ids) or any(not isinstance(v,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}',v) for v in ids):
            issues.append('Camera IDs must be unique letters, digits, underscores or hyphens')
        for camera in cameras:
            keys = ('name',) if role == 'server' else ('snapshot_url','username','password')
            if not isinstance(camera,dict) or any(not isinstance(camera.get(k),str) or not camera[k] for k in keys):
                issues.append('A camera is missing required fields')
    if role == 'collector' and not str(config.get('server_url','')).startswith('https://'):
        issues.append('Collector server_url must use HTTPS')
    if role == 'server':
        model_dir = Path(config.get('model_dir','/nonexistent'))
        # Hash the actual weights rather than trusting a stale download manifest.
        from scripts.download_models import SHA256
        import hashlib
        for name, digest in SHA256.items():
            file = model_dir / name
            if not file.is_file():
                issues.append('Missing model: '+name)
            elif hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                issues.append('Model checksum mismatch: '+name)
        origin = config.get('public_origin')
        if origin and (not isinstance(origin,str) or not origin.startswith('https://') or origin != config.get('public_base_url') or origin not in config.get('allowed_origins',[])):
            issues.append('Public origin must be HTTPS, match public_base_url and be allowed')
        data = Path(config.get('data_dir','/nonexistent'))
        if data.exists() and not os.access(data, os.W_OK):
            issues.append('Data directory is not writable by this user')
        elif not data.exists() and not os.access(data.parent, os.W_OK):
            issues.append('Create a private writable parent for the data directory')
    return list(dict.fromkeys(issues))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init','doctor'])
    parser.add_argument('role', choices=['server','collector'])
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.command == 'init':
        if args.config:
            parser.error('--config applies only to doctor')
        try:
            initialize(args.role)
        except (ValueError,OSError) as error:
            parser.exit(1, str(error)+'\n')
    else:
        path = args.config or ROOT / '.runtime' / ('config.json' if args.role == 'server' else 'collector-config.json')
        issues = diagnose(args.role,path)
        for issue in issues: print('CHECK: '+issue)
        if issues: parser.exit(1, 'Resolve these checks before starting the service. No credentials were printed.\n')
        print('Local configuration checks passed. Verify live connectivity and authentication separately.')


if __name__ == '__main__':
    main()
