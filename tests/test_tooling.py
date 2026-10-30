"""Meaningful checks for demo isolation, safe setup, and curated release contents."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from fastapi.testclient import TestClient
import pytest

from scripts.demo import demo_app
from scripts.manage import initialize, diagnose

ROOT=Path(__file__).resolve().parents[1]


def test_demo_ignores_live_config_blocks_external_host_and_cleans_up(monkeypatch):
    monkeypatch.setenv('HOME_MONITOR_CONFIG','/nonexistent/private/config.json')
    with demo_app() as app:
        private=app.state.store.root.parent
        with TestClient(app,base_url='http://127.0.0.1:8870') as client:
            assert client.get('/api/session').json()['authorized']
            status=client.get('/api/status').json()
            assert len(status['cameras'])==3
            assert all(camera['name'].startswith('Demo ') for camera in status['cameras'])
            assert len(client.get('/api/events').json())==24
            assert client.get('/api/cameras/camera-1/image').headers['content-type']=='image/jpeg'
            assert client.post('/api/ingest/camera-1/frame',content=b'private').status_code==403
            assert client.post('/api/push/test').status_code==403
            assert client.get('http://untrusted.invalid/api/status').status_code==400
            assert private.exists()
    assert not private.exists()


def test_init_preserves_config_and_doctor_never_prints_secrets(tmp_path):
    initialize('collector',tmp_path)
    config_path=tmp_path/'.runtime/collector-config.json'
    contents=config_path.read_bytes()
    assert config_path.stat().st_mode & 0o777==0o600
    assert config_path.parent.stat().st_mode & 0o777==0o700
    assert str(tmp_path) in (tmp_path/'.runtime/home-monitor-collector.service').read_text()
    with pytest.raises(ValueError,match='already exists'):
        initialize('collector',tmp_path)
    assert config_path.read_bytes()==contents
    assert diagnose('collector',config_path)
    config=json.loads(contents)
    # Invented non-routable configuration is sufficient for static validation.
    config.update(server_url='https://collector.test',collector_token='synthetic-sensitive-value')
    config['cameras']=[dict(id='camera-1',snapshot_url='http://camera.test/snapshot',username='synthetic-user',password='synthetic-password')]
    config_path.write_text(json.dumps(config))
    assert diagnose('collector',config_path)==[]
    config_path.chmod(0o644)
    errors=diagnose('collector',config_path)
    assert any('permissions' in error for error in errors)
    assert 'synthetic-sensitive-value' not in str(errors)


def test_release_uses_git_objects_excludes_untracked_private_decoy_and_is_deterministic(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    def git(*args):
        return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
    git('config','user.name','Synthetic Maintainer')
    git('config','user.email','maintainer@example.invalid')
    (repo/'README.md').write_text('Synthetic source\n')
    git('add','README.md'); git('commit','-qm','Synthetic fixture')
    (repo/'camera-password.txt').write_text('synthetic-private-decoy')
    env={**os.environ,'PYTHONPATH':str(ROOT/'scripts')}
    run=lambda out:subprocess.run([sys.executable,str(ROOT/'scripts/build_release.py'),'--version','v1.2.3','--output',str(out)],cwd=repo,env=env,check=True,capture_output=True)
    run(tmp_path/'one'); run(tmp_path/'two')
    a=tmp_path/'one/home-monitoring-v1.2.3.zip'
    assert a.read_bytes()==(tmp_path/'two'/a.name).read_bytes()
    with zipfile.ZipFile(a) as archive:
        assert set(archive.namelist())=={'home-monitoring-v1.2.3/README.md','home-monitoring-v1.2.3/BUILD-INFO.json'}
        assert b'synthetic-private-decoy' not in b''.join(archive.read(name) for name in archive.namelist())
    # Accidentally tracked runtime data must block packaging.
    (repo/'.runtime').mkdir(); (repo/'.runtime/config.json').write_text('{}')
    git('add','.runtime/config.json'); git('commit','-qm','Synthetic unsafe fixture')
    result=subprocess.run([sys.executable,str(ROOT/'scripts/build_release.py'),'--version','v1.2.3','--output',str(tmp_path/'blocked')],cwd=repo,env=env,capture_output=True)
    assert result.returncode!=0
    assert b'Publication guard failed' in result.stderr
    assert not (tmp_path/'blocked/home-monitoring-v1.2.3.zip').exists()


def test_collector_presence_reads_only_answering_neighbours(tmp_path):
    from collector import neighbours
    table = tmp_path / "arp"
    table.write_text("IP address HW type Flags HW address Mask Device\n192.0.2.20 0x1 0x2 AA:BB:CC:00:00:01 * eth0\n192.0.2.21 0x1 0x0 00:00:00:00:00:00 * eth0\n")
    assert neighbours(table) == {"aa:bb:cc:00:00:01"}


def test_release_workflow_creates_only_a_draft_after_checks():
    workflow=(ROOT/'.github/workflows/release.yml').read_text()
    assert 'needs: checks' in workflow and '--draft' in workflow and 'verify_release.py' in workflow
    assert 'workflow_call' in (ROOT/'.github/workflows/checks.yml').read_text()
