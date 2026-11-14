"""applet-homeproxy-1 under the fleetmux SDK (see conftest.applet_host)."""
import signal
import socket
import time

from claude_route.home import health_ok


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_home_applet_heartbeats_reports_phase_and_stops_clean(tmp_path, applet_host):
    client, start = applet_host
    port = free_port()
    params = tmp_path / 'claude_route.yaml'
    params.write_text('home:\n  port: %d\n  check_interval_s: 0.3\n' % port)
    proc, state = start('applet-homeproxy-1', 'claude_route.home', params, 'home')
    key = 'fx:applet:applet-homeproxy-1'
    deadline = time.time() + 15
    while not (health_ok(port)[0] and (client.hget(key, 'phase') or '').startswith('serving')):
        assert proc.poll() is None, proc.stderr.read()
        assert time.time() < deadline, client.hgetall(key)
        time.sleep(0.1)
    h = client.hgetall(key)
    assert int(h['pid']) == proc.pid and float(h['hb_at']) > 0
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(10) == 0
    assert not health_ok(port, timeout=1)[0]
    assert list(state.iterdir()) == []  # durable_state false: nothing kept


def test_home_applet_config_error_exits_78(tmp_path, applet_host):
    _, start = applet_host
    params = tmp_path / 'claude_route.yaml'
    params.write_text('home:\n  bind: 0.0.0.0\n')
    proc, _ = start('applet-homeproxy-1', 'claude_route.home', params, 'home')
    assert proc.wait(15) == 78
    assert proc.stderr.read().strip().splitlines()[-1].startswith('config error: bind')
