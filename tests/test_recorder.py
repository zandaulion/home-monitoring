from collections import namedtuple
import os

import recorder
from recorder import clip_due, is_memory, selected_segments, trim_buffer


def fill(folder, times):
    for at in times:
        (folder / f'{at}.mp4').touch()


def test_window_covers_the_whole_visit_and_excludes_open_file(tmp_path):
    fill(tmp_path, range(100, 300, 2))
    selected, complete = selected_segments(tmp_path, 140, 200)
    assert int(selected[0].stem) <= 118 and int(selected[-1].stem) >= 222
    assert complete
    assert tmp_path / '298.mp4' not in selected


def test_interrupted_footage_is_kept_and_flagged(tmp_path):
    fill(tmp_path, range(100, 300, 2))
    for at in range(130, 150, 2):
        (tmp_path / f'{at}.mp4').unlink()
    selected, complete = selected_segments(tmp_path, 140, 160)
    assert selected and not complete


def test_missing_history_or_ending_is_flagged(tmp_path):
    fill(tmp_path, range(100, 180, 2))
    assert selected_segments(tmp_path, 110, 120) == (selected_segments(tmp_path, 110, 120)[0], False)
    assert selected_segments(tmp_path, 110, 120)[0]
    assert not selected_segments(tmp_path, 150, 170)[1]


def test_nothing_recorded_gives_no_clip(tmp_path):
    assert selected_segments(tmp_path, 100, 120) == ([], False)


def job(start, end, through=None):
    return {'start': start, 'end': end, 'through': through}


def test_clip_waits_for_footage_past_the_end():
    assert not clip_due(job(100, 110), 140, 130)
    assert not clip_due(job(100, 110), 125, 139)
    assert clip_due(job(100, 110), 134, 139)
    assert clip_due(job(100, 110), 125, 161)


def test_long_visit_sends_early_evidence_once():
    assert clip_due(job(100, 400), 400, 146)
    assert not clip_due(job(100, 400, through=140), 400, 200)
    assert clip_due(job(100, 400, through=140), 430, 430)


def test_visit_ending_just_before_the_early_clip_is_clipped_again():
    assert clip_due(job(100, 138), 150, 145)
    assert not clip_due(job(100, 138, through=100), 150, 150)
    assert clip_due(job(100, 138, through=100), 162, 166)


def test_buffer_trims_by_capture_time(tmp_path):
    fill(tmp_path, [100, 1000, 1500])
    trim_buffer(tmp_path, 1600)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['1000.mp4', '1500.mp4']


def test_memory_check_uses_the_longest_mount(tmp_path, monkeypatch):
    mounts = tmp_path / 'mounts'
    mounts.write_text('/dev/sda1 / ext4 rw 0 0\ntmpfs /dev/shm tmpfs rw 0 0\n')
    real = recorder.Path

    class Fake(type(real())):
        def read_text(self):
            return mounts.read_text() if str(self) == '/proc/mounts' else super().read_text()

    monkeypatch.setattr(recorder, 'Path', Fake)
    assert is_memory('/dev/shm/home-monitor')
    assert not is_memory('/srv/recordings')


def test_usb_prune_expires_clips_and_old_continuous_footage(tmp_path, monkeypatch):
    old = tmp_path / 'camera-test' / '100.ts'
    clip = tmp_path / 'clips' / 'camera-test' / 'a.mp4'
    fresh = tmp_path / 'clips' / 'camera-test' / 'b.mp4'
    for path in (old, clip, fresh):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    os.utime(old, (100, 100)); os.utime(clip, (100, 100)); os.utime(fresh, (9_000_000, 9_000_000))
    disk = namedtuple('Disk', 'total used free')
    monkeypatch.setattr(recorder.shutil, 'disk_usage', lambda _: disk(1000, 1, 999))
    recorder.prune_usb(tmp_path, now=9_000_100, retention_seconds=86400, reserve_bytes=100)
    assert not old.exists() and not clip.exists() and fresh.exists()


def test_usb_clips_last_for_the_configured_days(tmp_path, monkeypatch):
    clip = tmp_path / 'clips' / 'camera-test' / 'a.mp4'
    clip.parent.mkdir(parents=True)
    clip.touch()
    os.utime(clip, (0, 0))
    disk = namedtuple('Disk', 'total used free')
    monkeypatch.setattr(recorder.shutil, 'disk_usage', lambda _: disk(1000, 1, 999))
    recorder.prune_usb(tmp_path, now=29*86400, retention_seconds=86400, reserve_bytes=100)
    assert clip.exists()
    recorder.prune_usb(tmp_path, now=31*86400, retention_seconds=86400, reserve_bytes=100)
    assert not clip.exists()
