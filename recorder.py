"""Optional alarm video recorder; uses FFmpeg and the standard library only.

Streams are kept in a short rolling buffer in memory, so a slow or missing USB
drive cannot interrupt them. Each event becomes a clip that goes to the server and
to the USB drive. Keep this config private: RTSP credentials and FFmpeg
diagnostics never enter logs.
"""
import argparse
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request

STOP = threading.Event()
LOG = logging.getLogger('recorder')
BUFFER_SECONDS = 660  # Longest visit clip, plus time to finish and retry it.
CLIP_BEFORE = 22
CLIP_AFTER = 23
SETTLE_SECONDS = 28  # Footage reaches the buffer late, and the last file stays open.
GIVE_UP_WAITING = 50
USB_CLIP_DAYS = 30
FIRST_CLIP_AFTER = 45  # Evidence leaves the house before a long visit ends.
SEGMENT_GAP = 12
STALL_SECONDS = 45
MAX_UPLOAD = 64_000_000
RESTART_SECONDS = 5


def stem(path):
    return int(path.stem)


def segments(folder):
    return sorted(folder.glob('*.mp4'), key=stem)


def is_memory(path):
    """True when the path is on tmpfs, so writing to it never touches a disk."""
    target = str(Path(path).resolve())
    best, kind = '', ''
    for line in Path('/proc/mounts').read_text().splitlines():
        mount, fstype = line.split()[1:3]
        if (target == mount or target.startswith(mount.rstrip('/')+'/')) and len(mount) > len(best):
            best, kind = mount, fstype
    return kind == 'tmpfs'


def selected_segments(folder, start, end):
    """Whole keyframe segments for a window, and whether nothing is missing.

    Interrupted footage is still evidence, so it is returned and flagged rather
    than refused. Never read the last file, which FFmpeg is still writing.
    """
    files = segments(folder)
    closed = files[:-1]
    earlier = [p for p in closed if stem(p) <= start-CLIP_BEFORE]
    first = stem(earlier[-1]) if earlier else start-CLIP_BEFORE
    chosen = [p for p in closed if first <= stem(p) <= end+CLIP_AFTER]
    reached_end = bool(files) and stem(files[-1]) >= end+CLIP_AFTER
    unbroken = all(stem(b)-stem(a) <= SEGMENT_GAP for a, b in zip(chosen, chosen[1:]))
    return chosen, bool(chosen and earlier and reached_end and unbroken)


def clip_due(job, newest, now):
    """Ready once footage has passed the window, or early for a long visit."""
    if job['through'] is None and now >= job['start']+FIRST_CLIP_AFTER:
        return True
    return now >= job['end']+SETTLE_SECONDS and (newest >= job['end']+CLIP_AFTER or now >= job['end']+GIVE_UP_WAITING)


def trim_buffer(folder, now):
    # File names are capture times, so this needs no disk metadata.
    for path in segments(folder):
        if stem(path) >= now-BUFFER_SECONDS:
            break
        path.unlink(missing_ok=True)


def prune_usb(root, now, retention_seconds, reserve_bytes, clip_days=USB_CLIP_DAYS):
    """Expire clips; also clear footage left by the earlier continuous recorder."""
    for path in (p for p in root.glob('*/*') if p.is_file() and p.parent.name != 'clips'):
        if now-path.stat().st_mtime > retention_seconds:
            path.unlink(missing_ok=True)
    for path in sorted(root.glob('clips/*/*.mp4'), key=lambda p: p.stat().st_mtime):
        if now-path.stat().st_mtime > clip_days*86400 or shutil.disk_usage(root).free < reserve_bytes:
            path.unlink(missing_ok=True)


def rtsp_address(camera):
    uri = urllib.parse.urlsplit(camera['rtsp_url'])
    host = '['+uri.hostname+']' if ':' in uri.hostname else uri.hostname
    auth = urllib.parse.quote(camera['username'], safe='')+':'+urllib.parse.quote(camera['password'], safe='')
    return urllib.parse.urlunsplit((uri.scheme, auth+'@'+host+(':'+str(uri.port) if uri.port else ''), uri.path, uri.query, ''))


def stop_process(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def record_stream(camera, config, buffer, state):
    """Keep FFmpeg running. Touches neither the network to the server nor USB."""
    cid = camera['id']
    folder = buffer / cid
    ffmpeg = os.path.expanduser(config['recording'].get('ffmpeg', 'ffmpeg'))
    proc = None
    started = 0
    try:
        while not STOP.is_set():
            now = time.time()
            folder.mkdir(parents=True, exist_ok=True)
            trim_buffer(folder, now)
            files = segments(folder)
            stalled = proc is not None and now-max([started]+[stem(p) for p in files[-1:]]) > STALL_SECONDS
            if not state['enabled'] or stalled:
                if stalled:
                    LOG.warning('%s stream stalled; restarting', cid)
                stop_process(proc)
                proc = None
            elif proc is None or proc.poll() is not None:
                if now-started >= RESTART_SECONDS:
                    command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp', '-timeout', '15000000', '-i', rtsp_address(camera), '-map', '0:v:0', '-an', '-c:v', 'copy', '-f', 'segment', '-segment_time', '2', '-reset_timestamps', '1', '-strftime', '1', str(folder/'%s.mp4')]
                    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    started = now
                    LOG.info('%s recording stream started', cid)
            STOP.wait(2)
    finally:
        stop_process(proc)


def build_clip(ffmpeg, selected, output):
    manifest = output.with_suffix('.concat')
    manifest.write_text(''.join("file '"+str(p)+"'\n" for p in selected))
    try:
        subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(manifest), '-map', '0:v:0', '-c:v', 'copy', '-movflags', '+faststart', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)
    finally:
        manifest.unlink(missing_ok=True)


def make_clips(camera, config, buffer, state, saves):
    """Ask the server what to clip. A slow server only delays clips, never video."""
    cid = camera['id']
    folder = buffer / cid
    work = buffer / 'clips'
    ffmpeg = os.path.expanduser(config['recording'].get('ffmpeg', 'ffmpeg'))
    base = config['server_url'].rstrip('/')+'/api/ingest/'+cid+'/recordings'
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    failures = 0

    def request(url, data=None):
        req = urllib.request.Request(url, data=data, headers={'Authorization': 'Bearer '+config['collector_token'], 'Content-Type': 'video/mp4'})
        with client.open(req, timeout=60) as response:
            return json.loads(response.read(1_000_000))

    while not STOP.is_set():
        try:
            newest = segments(folder)
            jobs = request(f'{base}?newest={stem(newest[-1]) if newest else 0}')
            state['enabled'] = jobs['monitoring_enabled']
            for job in jobs['requests']:
                if not re.fullmatch(r'[a-f0-9]{32}', job['event_id']):
                    continue
                files = segments(folder)
                now = time.time()
                if not files or not clip_due(job, stem(files[-1]), now):
                    continue
                end = min(job['end'], now-6)
                selected, complete = selected_segments(folder, job['start'], end)
                if not selected:
                    continue
                # An early clip marks only the start as done, so the visit is clipped again.
                through = end if now >= job['end']+SETTLE_SECONDS else job['start']
                work.mkdir(parents=True, exist_ok=True)
                output = work/(cid+'-'+job['event_id']+'.mp4')
                build_clip(ffmpeg, selected, output)
                if output.stat().st_size > MAX_UPLOAD:
                    LOG.warning('%s clip too large to attach', cid)
                else:
                    request(f"{base}/{job['event_id']}/video?through={through}&complete={str(complete).lower()}", output.read_bytes())
                    LOG.info('%s event clip attached', cid)
                saves.put((cid, job['event_id'], output))
            failures = 0
        except Exception as error:
            failures += 1
            if failures == 1 or failures % 200 == 0:
                LOG.warning('%s clip retry (%s)', cid, type(error).__name__)
        STOP.wait(3)


def save_to_usb(config, saves):
    """Copy finished clips at the drive's own pace; losing it costs only these copies."""
    mount = Path(config['recording']['mount'])
    root = mount / 'home-monitor'
    pruned = 0
    while not STOP.is_set():
        try:
            item = saves.get(timeout=3)
        except queue.Empty:
            item = None
        try:
            if item:
                cid, event_id, source = item
                try:
                    if mount.is_mount():
                        target = root/'clips'/cid/(event_id+'.mp4')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        partial = target.with_suffix('.tmp')
                        shutil.copyfile(source, partial)
                        partial.replace(target)
                finally:
                    source.unlink(missing_ok=True)
            now = time.time()
            if now-pruned > 300 and mount.is_mount():
                pruned = now
                prune_usb(root, now, config['recording'].get('retention_hours', 24)*3600, config['recording'].get('reserve_mb', 3072)*1024**2, config['recording'].get('clip_days', USB_CLIP_DAYS))
        except Exception as error:
            LOG.warning('USB save skipped (%s)', type(error).__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if not config['server_url'].startswith('https://'):
        raise SystemExit('The upstream server must use HTTPS')
    buffer = Path(config['recording'].get('buffer', '/tmp/home-monitor'))
    if not is_memory(buffer.parent):
        raise SystemExit('The recording buffer must be in memory (tmpfs)')
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    cameras = [camera for camera in config['cameras'] if camera.get('rtsp_url')]
    for camera in cameras:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}', camera['id']):
            raise ValueError('Invalid camera ID')
    saves = queue.Queue()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2*len(cameras)+1) as pool:
        futures = [pool.submit(save_to_usb, config, saves)]
        for camera in cameras:
            # Recording starts without waiting to hear from the server.
            state = {'enabled': True}
            futures.append(pool.submit(record_stream, camera, config, buffer, state))
            futures.append(pool.submit(make_clips, camera, config, buffer, state, saves))
        for future in futures:
            future.result()


if __name__ == '__main__':
    main()
