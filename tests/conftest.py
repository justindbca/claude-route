"""Shared fixtures.

- ``inventory`` (autouse): ``$FLEETMUX_SUPERPOWERS`` points at a temporary fleet-superpowers file, so no test
  reads or writes the real ``~/.fleetmux/fleet-superpowers/``. ``write_inventory`` replaces its content.
- ``applet_host``: run an applet module the way a fleetmux host does (FLEETMUX_APPLET_* environment, a private
  Redis on a Unix socket, the release's SDK on PYTHONPATH). Skipped when redis-server or the SDK is missing."""
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = Path(os.environ.get('FLEETMUX_RELEASE', Path.home() / 'fleetmux-current'))
HOME_MACHINE = 'home-machine'    # the home machine's inventory name; HOME_REACH are its ssh aliases
HOME_REACH = ['home-lan', 'home-relay', 'home-vpn']


def write_inventory(path, reach, machine=HOME_MACHINE):
    """A fleet-superpowers file with this machine's own entry and ``machine`` reached by ``reach`` in order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({'models': {}, 'machines': {
        'field-machine': {'source': 'self', 'reach': []},
        machine: {'source': 'copy', 'reach': [{'via': 'lan', 'ssh': d} for d in reach]}}}, sort_keys=False))
    return path


@pytest.fixture(autouse=True)
def inventory(tmp_path, monkeypatch):
    path = write_inventory(tmp_path / 'superpowers' / 'fleet-superpowers.yaml', HOME_REACH)
    monkeypatch.setenv('FLEETMUX_SUPERPOWERS', str(path))
    return path


@pytest.fixture
def applet_host(tmp_path):
    server = shutil.which('redis-server')
    if not server or not (RELEASE / 'fleetmux' / 'applet' / '__init__.py').exists():
        pytest.skip('needs redis-server and a fleetmux release at %s' % RELEASE)
    import redis
    short = Path(tempfile.mkdtemp(prefix='cr-', dir='/tmp'))  # AF_UNIX paths are limited to 104 bytes
    sock = short / 'redis.sock'
    proc = subprocess.Popen([server, '--port', '0', '--unixsocket', str(sock), '--unixsocketperm', '700',
                             '--save', '', '--appendonly', 'no', '--dir', str(tmp_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while not sock.exists():
        assert time.time() < deadline, 'redis-server did not start'
        time.sleep(0.05)
    client = redis.Redis(unix_socket_path=str(sock), decode_responses=True)
    started = []

    def start(name, module, params, block, extra_env=None):
        state, run = tmp_path / name / 'state', tmp_path / name / 'run'
        state.mkdir(parents=True)
        run.mkdir(parents=True)
        client.hset('fx:applet:' + name, mapping={'state': 'starting', 'started_at': repr(time.time())})
        env = dict(os.environ, FLEETMUX_APPLET_NAME=name, FLEETMUX_APPLET_INTERFACE='1',
                   FLEETMUX_APPLET_CONFIG=str(tmp_path / 'config.json'), FLEETMUX_APPLET_REDIS=str(sock),
                   FLEETMUX_APPLET_TOKEN=secrets.token_hex(16), FLEETMUX_APPLET_STATE=str(state),
                   FLEETMUX_APPLET_RUN=str(run), FLEETMUX_APPLET_HEARTBEAT_S='0.5',
                   FLEETMUX_APPLET_STALE_AFTER_S='5', FLEETMUX_APPLET_STOP_GRACE_S='10',
                   FLEETMUX_APPLET_PARAMS_FILE=str(params), FLEETMUX_APPLET_PARAMS_BLOCK=block,
                   PYTHONPATH=os.pathsep.join([str(RELEASE), str(ROOT)]), **(extra_env or {}))
        p = subprocess.Popen([sys.executable, '-m', module], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        started.append(p)
        return p, state

    yield client, start
    for p in started:
        if p.poll() is None:
            p.kill()
            p.wait()
    proc.terminate()
    proc.wait(10)
    shutil.rmtree(short, ignore_errors=True)
