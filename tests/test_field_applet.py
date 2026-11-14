"""applet-tunnel-1 under the fleetmux SDK with a fake ssh and a real home proxy as identity server.

The ssh targets come from the temporary inventory of conftest.inventory; the real ~/.fleetmux is never read."""
import os
import re
import signal
import sys
import threading
import time

import pytest
import yaml

from claude_route import EXIT_CONFIG
from claude_route.field import port_open
from claude_route.home import HomeProxy, health_ok
from conftest import HOME_MACHINE, RELEASE, ROOT
from test_field import FAKE_SSH, alive, free_port, free_range

KEY = 'fx:applet:applet-tunnel-1'


def params_file(tmp_path, port, remote_port, **kw):
    fake = tmp_path / 'fake_ssh.py'
    fake.write_text(FAKE_SSH)
    params = tmp_path / 'claude_route.yaml'
    params.write_text(yaml.safe_dump({'field': {
        'machine': HOME_MACHINE, 'local_port': port, 'remote_port': remote_port, 'ssh': [sys.executable, str(fake)],
        'route_ports': free_range(), 'restart_delay_s': 0.2, 'check_interval_s': 0.3, 'attempt_timeout_s': 2,
        **kw}}))
    return params


def wait_phase_up(client, proc, timeout=15):
    deadline = time.time() + timeout
    while not (client.hget(KEY, 'phase') or '').startswith('up:'):
        assert proc.poll() is None, proc.stderr.read()
        assert time.time() < deadline, client.hgetall(KEY)
        time.sleep(0.1)


def test_field_applet_up_then_sigterm_leaves_no_child_or_listener(tmp_path, applet_host):
    client, start = applet_host
    home = HomeProxy(port=0)
    home.start()
    port, pids = free_port(), tmp_path / 'pids'
    params = params_file(tmp_path, port, home.port)
    try:
        proc, state = start('applet-tunnel-1', 'claude_route.field', params, 'field',
                            {'FAKE_SSH_PIDS': str(pids)})
        wait_phase_up(client, proc)
        phase = client.hget(KEY, 'phase')
        assert re.match(r'up: via (home-lan|home-relay|home-vpn) \d+ms \(fail \d+/\d+, drops 0\) '
                        r'\| switched never \| standby: ', phase), phase  # an inventory target
        assert health_ok(port)[0]
        ssh_pids = [int(x) for x in pids.read_text().split()]
        ssh_pid = ssh_pids[0]
        assert os.getpgid(ssh_pid) == os.getpgid(proc.pid)  # the host's group sweep reaches the child
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == 0
        assert not port_open(port)
        deadline = time.time() + 5
        while any(alive(pid) for pid in ssh_pids):  # every route's ssh
            assert time.time() < deadline
            time.sleep(0.05)
        assert list(state.iterdir()) == []
    finally:
        home.close()


def test_session_is_held_at_launch_while_the_tunnel_is_down(tmp_path, applet_host, inventory):
    """Item 6 on the release's own launch wait (fleetmux.session_env, 40a8086c): no target connects -> the
    applet exits 78 after max_failed_rounds with the reason as its last stderr line, the port stays closed, and a Claude launch waits
    with route_wait=1. It starts only once the tunnel is up, with the tunnel's proxy and no other."""
    client, start = applet_host
    if str(RELEASE) not in sys.path:
        sys.path.insert(0, str(RELEASE))
    session_env = pytest.importorskip('fleetmux.session_env')
    row = yaml.safe_load((ROOT / 'teams' / 'field-applet.yaml').read_text())['applets'][0]
    port = free_port()
    vars_ = {k: v.replace(str(row['session_env']['port']), str(port)) for k, v in row['session_env']['vars'].items()}
    cfg = {'session_env': [{'applet': row['name'], 'port': port, 'vars': vars_, 'clis': ['claude']}]}

    down = tmp_path / 'down'
    down.write_text('home-lan\nhome-relay\nhome-vpn\n')
    home = HomeProxy(port=0)
    home.start()
    params = params_file(tmp_path, port, home.port, max_failed_rounds=2)
    slot = 'fx:agent:worker-1'
    client.hset(slot, mapping={'generation': 1, 'state': 'starting'})
    waited = []
    launcher = threading.Thread(target=lambda: waited.append(session_env.wait(
        cfg, 'claude', client, slot, 1, ready_s=60, interval=0.1, timeout=0.2)), daemon=True)
    try:
        proc, _ = start('applet-tunnel-1', 'claude_route.field', params, 'field', {'FAKE_SSH_DOWN': str(down)})
        assert proc.wait(30) == EXIT_CONFIG
        last = proc.stderr.read().strip().splitlines()[-1]
        assert last.startswith('tunnel: no ssh target of machine home-machine connects in 2 rounds in a row '
                               '(last round, tried in order: home-lan: ')
        assert not port_open(port)

        launcher.start()
        deadline = time.time() + 5
        while client.hget(slot, 'route_wait') != '1':
            assert time.time() < deadline, client.hgetall(slot)
            time.sleep(0.05)
        time.sleep(0.5)
        assert launcher.is_alive() and not waited  # held at launch, no direct start

        down.write_text('home-lan\n')  # the second registered target comes back
        proc, _ = start('applet-tunnel-2', 'claude_route.field', params, 'field', {'FAKE_SSH_DOWN': str(down)})
        launcher.join(15)
        assert waited == [True] and client.hget(slot, 'route_wait') == '0'
        assert health_ok(port)[0]
        env = session_env.environ(cfg, 'claude', {'HTTPS_PROXY': 'http://127.0.0.1:7890',
                                                  'https_proxy': 'http://127.0.0.1:7890',
                                                  'ALL_PROXY': 'socks5://127.0.0.1:7890'})
        url = 'http://127.0.0.1:%d' % port
        assert env == {'HTTPS_PROXY': url, 'HTTP_PROXY': url,
                       'NO_PROXY': 'localhost,127.0.0.1,::1', 'no_proxy': 'localhost,127.0.0.1,::1'}
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == 0
    finally:
        home.close()
