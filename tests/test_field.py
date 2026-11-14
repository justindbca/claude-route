import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from claude_route import EXIT_CONFIG, ConfigError, read_block
from claude_route.field import DEFAULTS, RESERVED_PORTS, Tunnel, port_open, reach
from claude_route.home import HomeProxy
from conftest import HOME_MACHINE, HOME_REACH, write_inventory

ROOT = Path(__file__).resolve().parents[1]

# A stand-in for ssh: checks the fixed options, then does what `ssh -N -L lh:lp:rh:rp dest` does locally
# (listen on lh:lp, forward each connection to rh:rp). Bind failure exits 255 like ExitOnForwardFailure.
# FAKE_SSH_LOG: append the destination of every start. FAKE_SSH_DOWN: a file listing destinations that do
# not connect (exit 255), read at each start.
FAKE_SSH = r'''
import os, signal, socket, sys, threading
args = sys.argv[1:]
opts = [args[i + 1] for i, a in enumerate(args) if a == '-o']
for need in ('BatchMode=yes', 'ExitOnForwardFailure=yes', 'ControlMaster=no', 'ControlPath=none'):
    if need not in opts:
        sys.exit(2)
dest = args[-1]
if os.environ.get('FAKE_SSH_LOG'):
    with open(os.environ['FAKE_SSH_LOG'], 'a') as f:
        f.write(dest + '\n')
down = os.environ.get('FAKE_SSH_DOWN')
if down and os.path.exists(down) and dest in open(down).read().split():
    sys.exit(255)
if os.environ.get('FAKE_SSH_EXIT'):
    sys.exit(int(os.environ['FAKE_SSH_EXIT']))
lh, lp, rh, rp = args[args.index('-L') + 1].split(':')
if os.environ.get('FAKE_SSH_PIDS'):
    with open(os.environ['FAKE_SSH_PIDS'], 'a') as f:
        f.write('%d\n' % os.getpid())
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    srv.bind((lh, int(lp)))
except OSError:
    sys.exit(255)
srv.listen()
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
while True:
    c, _ = srv.accept()
    try:
        u = socket.create_connection((rh, int(rp)), timeout=3)
    except OSError:
        c.close()
        continue
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
'''


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def free_range(n=16):
    """[first, last] of ``n`` consecutive loopback ports that nothing listens on now (route_ports of a test)."""
    import random
    for _ in range(200):
        low = random.randrange(20000, 60000 - n)
        if all(not port_open(p) for p in range(low, low + n)):
            return [low, low + n - 1]
    raise AssertionError('no free port range')


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.fixture
def fake_ssh(tmp_path):
    path = tmp_path / 'fake_ssh.py'
    path.write_text(FAKE_SSH)
    return [sys.executable, str(path)]


@pytest.fixture
def home_proxy():
    p = HomeProxy(port=0)
    p.start()
    yield p
    p.close()


def wait_for(cond, timeout=10, every=0.05):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, 'timed out'
        time.sleep(every)


def run_steps(t, until, timeout=10):
    deadline = time.time() + timeout
    while not until():
        assert time.time() < deadline, 'timed out: phase=%s' % t.phase
        time.sleep(min(t.step(), 0.1))


def make(fake_ssh, remote_port, inventory, reach_list=('a', 'b'), **kw):
    """A Tunnel built from a block, its targets from a temporary inventory listing ``reach_list``."""
    write_inventory(inventory, list(reach_list))
    block = {'machine': HOME_MACHINE, 'local_port': free_port(), 'remote_port': remote_port, 'ssh': fake_ssh,
             'route_ports': free_range(),
             'restart_delay_s': 0.2, 'check_interval_s': 0.1, 'stop_wait_s': 3, 'attempt_timeout_s': 3}
    block.update(kw)
    return Tunnel.from_block(block)


@pytest.fixture
def ssh_env(tmp_path, monkeypatch):
    """(start log, down file) of the fake ssh."""
    started, down = tmp_path / 'started', tmp_path / 'down'
    monkeypatch.setenv('FAKE_SSH_LOG', str(started))
    monkeypatch.setenv('FAKE_SSH_DOWN', str(down))
    return started, down


def lines(path):
    return path.read_text().split() if path.exists() else []


def per_round(path, n):
    """The start log as one sorted chunk of ``n`` per round: the routes of a round start together, so their
    fake ssh processes write the log in any order."""
    got = lines(path)
    return [sorted(got[i:i + n]) for i in range(0, len(got), n)]


def steps_until_exit(t, timeout=15):
    with pytest.raises(SystemExit) as exc:
        run_steps(t, lambda: False, timeout)
    return exc.value.code


# ---------------------------------------------------------------------- configuration
def test_argv_has_fixed_options_and_loopback_forward():
    t = Tunnel(HOME_MACHINE, ['home-lan'], ssh_options=['UserKnownHostsFile=/tmp/kh'])
    argv = t.argv('home-relay', 17900)
    assert argv[:3] == ['ssh', '-N', '-T']
    opts = [argv[i + 1] for i, a in enumerate(argv) if a == '-o']
    assert opts[:7] == ['BatchMode=yes', 'ExitOnForwardFailure=yes', 'ServerAliveInterval=15',
                        'ServerAliveCountMax=3', 'ConnectTimeout=10', 'ControlMaster=no', 'ControlPath=none']
    assert opts[7] == 'UserKnownHostsFile=/tmp/kh'  # extras come after: ssh keeps the first value
    assert argv[-3:] == ['-L', '127.0.0.1:17900:127.0.0.1:17891', 'home-relay']  # a route port, not local_port


def test_ssh_child_stays_in_the_applet_process_group(fake_ssh, home_proxy, inventory, ssh_env):
    """No setsid / start_new_session: the host stops the applet with killpg, which must reach ssh too."""
    t = make(fake_ssh, home_proxy.port, inventory, ['a'])
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        assert os.getpgid(t.child.pid) == os.getpgid(0) and os.getsid(t.child.pid) == os.getsid(0)
    finally:
        t.close()


def test_machine_has_no_default_and_the_project_block_sets_the_example_name():
    assert DEFAULTS['machine'] is None
    assert read_block(ROOT / 'claude_route.yaml', 'field')['machine'] == HOME_MACHINE == 'home-machine'


MACHINE_MISSING = ('machine is not set: set field.machine in claude_route.yaml to the home machine\'s name in the '
                   'machines block of the fleet-superpowers inventory (fleetmux superpowers show --machines)')


@pytest.mark.parametrize('block', [{}, {'machine': None}, {'local_port': 17892}])
def test_missing_machine_is_an_explicit_config_error(block):
    with pytest.raises(ConfigError) as exc:
        Tunnel.from_block(block)
    assert str(exc.value) == MACHINE_MISSING


@pytest.mark.parametrize('content', [None, 'field:\n  local_port: 17892\n'])
def test_standalone_process_without_machine_exits_78_and_says_so(tmp_path, content):
    argv = [sys.executable, '-m', 'claude_route.field']
    if content is not None:
        params = tmp_path / 'p.yaml'
        params.write_text(content)
        argv += ['--params', str(params)]
    proc = subprocess.run(argv, cwd=ROOT, stderr=subprocess.PIPE, text=True, timeout=30)
    assert proc.returncode == EXIT_CONFIG
    assert proc.stderr.strip().splitlines()[-1] == 'config error: ' + MACHINE_MISSING


def test_project_block_reads_targets_from_the_inventory_in_order(inventory):
    t = Tunnel.from_block(read_block(ROOT / 'claude_route.yaml', 'field'))
    assert (t.machine, t.local_port, t.remote_port) == (HOME_MACHINE, 17892, 17891)
    assert t.targets == HOME_REACH
    assert 'dest' not in DEFAULTS  # no ssh target outside the inventory


def test_inventory_order_is_kept(inventory):
    write_inventory(inventory, ['user@192.0.2.9', 'home-vpn', 'home-lan'])
    assert reach(HOME_MACHINE) == ['user@192.0.2.9', 'home-vpn', 'home-lan']


def test_inventory_default_path_is_fleetmux_rule(monkeypatch):
    from claude_route.field import inventory_path
    monkeypatch.delenv('FLEETMUX_SUPERPOWERS')
    assert inventory_path() == Path.home() / '.fleetmux' / 'fleet-superpowers' / 'fleet-superpowers.yaml'


@pytest.mark.parametrize('content, match', [
    (None, 'cannot read the fleet-superpowers inventory'),
    ('machines: {}\n', 'no machine home-machine'),
    ('machines:\n  other: {reach: [{ssh: x}]}\n', r'no machine home-machine .*machines: other'),
    ('machines:\n  home-machine: {reach: []}\n', 'lists no ssh destination'),
    ('machines:\n  home-machine: {source: copy}\n', 'lists no ssh destination'),
    ('machines:\n  home-machine: {reach: [{ssh: -oProxyCommand=x}]}\n', r'reach\[0\]\.ssh'),
    ('machines:\n  home-machine: {reach: [{ssh: "a b"}]}\n', r'reach\[0\]\.ssh'),
    (': [\n', 'cannot read the fleet-superpowers inventory'),
])
def test_unusable_inventory_is_a_config_error(inventory, content, match):
    if content is None:
        inventory.unlink()
    else:
        inventory.write_text(content)
    with pytest.raises(ConfigError, match=match):
        Tunnel.from_block({'machine': HOME_MACHINE})


@pytest.mark.parametrize('block, match', [
    ({'dest': 'home-lan'}, 'unknown key dest'),
    ({'machine': '-x'}, 'machine'),
    ({'machine': 'a b'}, 'machine'),
    ({'ssh_options': ['ProxyCommand x']}, 'ssh_options'),
    ({'local_port': 22}, 'local_port'),
    ({'tunnel': 1}, 'unknown key'),
    ({'server_alive_count_max': 0}, 'server_alive_count_max'),
    ({'attempt_timeout_s': 0}, 'attempt_timeout_s'),
])
def test_bad_blocks_are_refused(block, match):
    with pytest.raises(ConfigError, match=match):
        Tunnel.from_block(dict({'machine': HOME_MACHINE}, **block))


@pytest.mark.parametrize('port', [7890, 9090, 9091, 17890, 19091, 18888, 16379, 16380, 18790, 18793, 14001])
def test_reserved_field_ports_are_refused(port):
    assert port in RESERVED_PORTS
    with pytest.raises(ConfigError, match='local_port %d is reserved' % port):
        Tunnel.from_block({'machine': HOME_MACHINE, 'local_port': port})


def test_default_and_project_port_avoid_the_reserved_ports():
    field = read_block(ROOT / 'claude_route.yaml', 'field')
    assert DEFAULTS['local_port'] not in RESERVED_PORTS and field['local_port'] not in RESERVED_PORTS


def test_session_env_row_matches_the_field_block():
    rows = yaml.safe_load((ROOT / 'teams' / 'field-applet.yaml').read_text())['applets']
    assert len(rows) == 1
    row = rows[0]
    field = read_block(ROOT / 'claude_route.yaml', 'field')
    assert row['name'] == 'applet-tunnel-1' and row['command'] == ['{python}', '-m', 'claude_route.field']
    assert row['params'] == {'file': '../claude_route.yaml', 'block': 'field'} and row['durable_state'] is False
    env = row['session_env']
    url = 'http://127.0.0.1:%d' % field['local_port']
    assert env == {'port': field['local_port'], 'clis': ['claude'], 'vars': {
        'HTTPS_PROXY': url, 'HTTP_PROXY': url, 'NO_PROXY': 'localhost,127.0.0.1,::1'}}


# ---------------------------------------------------------------------- targets in order (fake ssh)
def test_first_target_that_connects_is_used(fake_ssh, home_proxy, inventory, ssh_env, capfd):
    started, down = ssh_env
    t = make(fake_ssh, home_proxy.port, inventory, ['a', 'b', 'c'])
    t.start()
    try:
        run_steps(t, lambda: t.healthy and not t.startup)
        # every route is started and measured; the first one that connects is used, and equal routes (all on
        # the loopback here) do not beat it by the margin
        first = t.dest
        assert per_round(started, 3) == [['a', 'b', 'c']] and t.phase.startswith('up: via %s ' % first)
    finally:
        t.close()
    switched = [x for x in capfd.readouterr().err.splitlines() if x.startswith('tunnel: switch ')]
    assert switched == ['tunnel: switch none -> %s: startup' % first]
    assert not port_open(t.local_port)


def test_targets_are_tried_in_registered_order(fake_ssh, home_proxy, inventory, ssh_env, capfd):
    started, down = ssh_env
    down.write_text('a\nb\n')
    t = make(fake_ssh, home_proxy.port, inventory, ['a', 'b', 'c'])
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        assert t.dest == 'c' and per_round(started, 3) == [['a', 'b', 'c']]
    finally:
        t.close()
    starts = [x.split()[-1] for x in capfd.readouterr().err.splitlines() if x.startswith('tunnel: start (target')]
    assert starts == ['a', 'b', 'c']  # the applet starts them in registered order


def test_no_target_connects_exits_78_with_explicit_error(fake_ssh, inventory, ssh_env, capfd):
    started, down = ssh_env
    down.write_text('a\nb\n')
    t = make(fake_ssh, free_port(), inventory, ['a', 'b'], max_failed_rounds=2)
    t.start()
    try:
        assert steps_until_exit(t) == EXIT_CONFIG
    finally:
        t.close()
    assert per_round(started, 2) == [['a', 'b']] * 2  # each once per round, max_failed_rounds, no other route
    assert t.phase.startswith('no route:') and not port_open(t.local_port)
    last = capfd.readouterr().err.strip().splitlines()[-1]
    assert last.startswith('tunnel: no ssh target of machine home-machine connects in 2 rounds in a row '
                           '(last round, tried in order: a: ssh exited')
    assert '; b: ssh exited with 255' in last


def test_target_without_home_proxy_identity_is_not_used(fake_ssh, inventory, ssh_env):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'service': 'not-the-proxy'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(('127.0.0.1', 0), Other)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    started, _ = ssh_env
    t = make(fake_ssh, srv.server_address[1], inventory, ['a', 'b'], attempt_timeout_s=1, max_failed_rounds=1)
    t.start()
    try:
        assert steps_until_exit(t) == EXIT_CONFIG
        assert per_round(started, 2) == [['a', 'b']] and not t.healthy
        assert 'no healthy forward within 1 s' in t.fatal
    finally:
        t.close()
        srv.shutdown()
        srv.server_close()
    assert not port_open(t.local_port)


def test_drop_reconnects_the_same_target_first(fake_ssh, home_proxy, inventory, ssh_env):
    started, _ = ssh_env
    t = make(fake_ssh, home_proxy.port, inventory, ['a'])  # with more routes a drop moves to another one
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        first = t.child.pid
        os.kill(first, signal.SIGKILL)
        run_steps(t, lambda: t.healthy and t.child.pid != first)
        assert lines(started) == ['a', 'a'] and t.dest == 'a' and t.starts == 2
    finally:
        t.close()


def test_drop_then_reconnect_fails_moves_to_the_next_target(fake_ssh, home_proxy, inventory, ssh_env):
    started, down = ssh_env
    t = make(fake_ssh, home_proxy.port, inventory, ['a', 'b', 'c'])
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        down.write_text('a\n')
        os.kill(t.child.pid, signal.SIGKILL)
        run_steps(t, lambda: t.healthy and t.dest != 'a')
        # the next target is the best other route (b and c are equal here); it was up already: no new ssh
        assert t.dest in ('b', 'c') and per_round(started, 3) == [['a', 'b', 'c']]
    finally:
        t.close()


def test_drop_on_the_last_target_wraps_to_the_first(fake_ssh, home_proxy, inventory, ssh_env):
    started, down = ssh_env
    down.write_text('a\n')
    t = make(fake_ssh, home_proxy.port, inventory, ['a', 'b'])
    t.start()
    try:
        run_steps(t, lambda: t.healthy and t.dest == 'b')
        down.write_text('b\n')
        os.kill(t.child.pid, signal.SIGKILL)
        run_steps(t, lambda: t.healthy and t.dest == 'a')
        assert per_round(started, 2) == [['a', 'b']] * 2  # no route left: a round starts every target
    finally:
        t.close()


def test_drop_with_every_target_down_exits_78(fake_ssh, home_proxy, inventory, ssh_env):
    started, down = ssh_env
    t = make(fake_ssh, home_proxy.port, inventory, ['a', 'b'], max_failed_rounds=2)
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        down.write_text('a\nb\n')
        for dest in ('a', 'b'):  # every route drops
            os.kill(t.route(dest).child.pid, signal.SIGKILL)
        assert steps_until_exit(t) == EXIT_CONFIG
        assert per_round(started, 2) == [['a', 'b']] * 3  # two rounds of every target
    finally:
        t.close()
    assert not port_open(t.local_port)


def test_home_proxy_gone_is_a_drop_and_ends_with_78(fake_ssh, inventory, ssh_env):
    home = HomeProxy(port=0)
    home.start()
    started, _ = ssh_env
    t = make(fake_ssh, home.port, inventory, ['a'], unhealthy_restart_s=1, attempt_timeout_s=1, max_failed_rounds=2)
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        home.close()
        assert steps_until_exit(t) == EXIT_CONFIG
        assert lines(started) == ['a', 'a', 'a']
    finally:
        t.close()
    assert not port_open(t.local_port)


def test_inventory_is_read_again_for_a_reconnect_round(fake_ssh, home_proxy, inventory, ssh_env):
    started, down = ssh_env
    t = make(fake_ssh, home_proxy.port, inventory, ['a'])
    t.start()
    try:
        run_steps(t, lambda: t.healthy)
        write_inventory(inventory, ['a', 'tailnet'])  # a target registered while the applet runs
        down.write_text('a\n')
        os.kill(t.child.pid, signal.SIGKILL)
        run_steps(t, lambda: t.healthy and t.dest == 'tailnet')
        assert lines(started)[0] == 'a' and sorted(lines(started)[1:]) == ['a', 'tailnet']
    finally:
        t.close()


def test_port_used_by_another_program_exits_78(fake_ssh, inventory, ssh_env):
    started, _ = ssh_env
    other = socket.socket()
    other.bind(('127.0.0.1', 0))
    other.listen()
    try:
        t = make(fake_ssh, free_port(), inventory, ['a'], local_port=other.getsockname()[1])
        t.start()
        assert steps_until_exit(t) == EXIT_CONFIG
        assert 'already in use by another program' in t.fatal and lines(started) == []
    finally:
        other.close()


def test_close_kills_ssh_and_confirms_listener_gone(fake_ssh, home_proxy, inventory, ssh_env):
    t = make(fake_ssh, home_proxy.port, inventory)
    t.start()
    run_steps(t, lambda: t.healthy)
    child = t.child
    t.close()
    assert child.poll() is not None and t.child is None
    assert not port_open(t.local_port)


# ---------------------------------------------------------------------- standalone process
def field_params(tmp_path, port, remote_port, fake_ssh, **kw):
    params = tmp_path / 'p.yaml'
    block = {'machine': HOME_MACHINE, 'local_port': port, 'remote_port': remote_port, 'ssh': fake_ssh,
             'restart_delay_s': 0.2,
             'check_interval_s': 0.2, 'attempt_timeout_s': 2, 'route_ports': free_range()}
    block.update(kw)
    params.write_text(yaml.safe_dump({'field': block}))
    return params


def test_standalone_process_sigterm_leaves_nothing(tmp_path, fake_ssh, home_proxy, inventory):
    port, pids = free_port(), tmp_path / 'pids'
    params = field_params(tmp_path, port, home_proxy.port, fake_ssh)
    low, high = yaml.safe_load(params.read_text())['field']['route_ports']
    env = dict(os.environ, FAKE_SSH_PIDS=str(pids))
    proc = subprocess.Popen([sys.executable, '-m', 'claude_route.field', '--params', str(params)], cwd=ROOT,
                            env=env, stderr=subprocess.PIPE, text=True)
    try:
        wait_for(lambda: pids.exists() and len(pids.read_text().split()) == len(HOME_REACH) and port_open(port))
        from claude_route.home import health_ok
        wait_for(lambda: health_ok(port)[0])
        ssh_pids = [int(x) for x in pids.read_text().split()]  # one ssh per route
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == 0
        err = proc.stderr.read()
        assert 'closed; pid' in err and 'tunnel: start (target 1/3)' in err and err.rstrip().endswith('exits')
        assert not port_open(port)
        assert not [p for p in range(low, high + 1) if port_open(p)]  # every route port refuses
        wait_for(lambda: not any(alive(pid) for pid in ssh_pids), timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_standalone_process_with_no_route_exits_78_and_says_why(tmp_path, fake_ssh, inventory, ssh_env):
    started, down = ssh_env
    down.write_text('\n'.join(HOME_REACH))
    port = free_port()
    params = field_params(tmp_path, port, free_port(), fake_ssh, max_failed_rounds=2)
    proc = subprocess.run([sys.executable, '-m', 'claude_route.field', '--params', str(params)], cwd=ROOT,
                          stderr=subprocess.PIPE, text=True, timeout=30)
    assert proc.returncode == EXIT_CONFIG
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith('tunnel: no ssh target of machine home-machine connects in 2 rounds in a row '
                           '(last round, tried in order: home-lan: ')
    assert per_round(started, 3) == [sorted(HOME_REACH)] * 2 and not port_open(port)
