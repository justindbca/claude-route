"""applet-tunnel-1 picks the fastest and most stable route to home and switches with a gate.

A fake world with no process, socket or sleep: a fake ssh per route (``subprocess.Popen`` stand-in) that
connects after a per-destination delay, a fake clock, a fake health probe with a per-destination latency, and
a stand-in for ``claude_route.relay.LoopbackRelay`` that records whether ``local_port`` listens and where a new
connection would go. The probe (``Tunnel.probe``) and its executor (``Tunnel.executor``) are injected: the
executor runs the probe inline, so every result is in before ``step()`` returns; no thread, no timing.

``World.latency[dest]``: seconds (float), ``None`` (the request times out), a list (one value per probe,
cycling) or a callable of the fake clock's time.
"""
import itertools
import re
import signal
import time

import pytest
import yaml

import claude_route
from claude_route import EXIT_CONFIG, ConfigError, read_block
from claude_route import field
from claude_route.field import DEFAULTS, RESERVED_PORTS, Tunnel
from conftest import HOME_MACHINE, write_inventory
from test_field import ROOT

TARGETS = ['a', 'b', 'c', 'd']
WALL = 1763100000.0
NEW_PARAMS = {'route_ports': [17900, 17915], 'probe_timeout_s': 5, 'samples': 5, 'sample_gap_s': 1,
              'measure_interval_s': 60, 'window_s': 600, 'fail_penalty_ms': 5000, 'drop_penalty_ms': 2000,
              'switch_margin_ratio': 0.3, 'switch_margin_ms': 50, 'switch_hold_s': 120, 'switch_cooldown_s': 300,
              'failover_after_fails': 3}
MEASURE_RE = re.compile(r'tunnel: measure (\S+) port (\d+) ok (\d+)/(\d+) median (\d+ms|n/a) fail (\d+)/(\d+) '
                        r'drops (\d+) cost (\d+ms|inf) (current|standby)( last: .+)?\Z')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeSsh:
    """``ssh -N -L 127.0.0.1:<port>:...`` to ``dest``: connects ``connect_delay[dest]`` seconds after the start
    unless the destination refuses (the process exits by itself) or hangs (never connects)."""

    def __init__(self, world, argv):
        self.world, self.argv, self.dest = world, list(argv), argv[-1]
        self.port = int(argv[argv.index('-L') + 1].split(':')[1])
        self.pid, self.started_at = next(world.pids), world.clock()
        self.returncode = None

    @property
    def connected(self):
        w = self.world
        return (self.returncode is None and self.dest not in w.refuse and self.dest not in w.hang
                and w.clock() >= self.started_at + w.connect_delay.get(self.dest, 0))

    def poll(self):
        if self.returncode is None and self.dest in self.world.refuse:
            self.returncode = self.world.refuse[self.dest]
        return self.returncode

    def exit(self, code):
        self.returncode = code

    def send_signal(self, sig):
        assert sig == signal.SIGTERM
        if self.returncode is None:
            self.returncode = 0 if self.connected else -signal.SIGTERM

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self.returncode = -signal.SIGKILL


class FakeRelay:
    def __init__(self, world, port, choose, on_route_refused):
        self.world, self.port, self.choose, self.on_route_refused = world, port, choose, on_route_refused
        self.listening, self.opens, self.closes, self.shut = False, 0, 0, False

    def open(self):
        if self.world.port_taken:
            raise OSError(48, 'Address already in use')
        self.listening = True
        self.opens += 1

    def close(self):
        if self.listening:
            self.closes += 1
        self.listening = False

    def shutdown(self):
        self.listening, self.shut = False, True

    def new_connection_port(self):
        ports = self.choose()
        return ports[0] if ports else None


class World:
    def __init__(self, clock):
        self.clock = clock
        self.refuse, self.hang, self.connect_delay, self.latency = {}, set(), {}, {}
        self.children, self.pids, self.probes = [], itertools.count(5000), []
        self.relay, self.port_taken, self.inventory, self.timeout = None, False, None, 5
        self._turn = {}

    def popen(self, argv, **_):
        child = FakeSsh(self, argv)
        self.children.append(child)
        return child

    @property
    def started(self):
        return [c.dest for c in self.children]

    def alive(self, dest):
        return [c for c in self.children if c.dest == dest and c.poll() is None]

    def on_port(self, port):
        for c in self.children:
            if c.port == port and c.poll() is None and c.connected:
                return c
        return None

    def listening(self, port=None, **_):
        if self.relay is not None and port == self.relay.port:
            return self.relay.listening
        return self.on_port(port) is not None

    def next_latency(self, dest):
        spec = self.latency.get(dest, 0.05)
        if callable(spec):
            return spec(self.clock())
        if isinstance(spec, list):
            n = self._turn.get(dest, 0)
            self._turn[dest] = n + 1
            return spec[n % len(spec)]
        return spec

    def probe(self, port):
        """``Tunnel.probe``: (ok, info, elapsed_ms) through the route port, after the destination's latency."""
        child = self.on_port(port)
        if child is None:
            return False, 'no answer: [Errno 61] Connection refused', 0.0
        seconds = self.next_latency(child.dest)
        self.probes.append((self.clock(), child.dest, seconds))
        if seconds is None or seconds > self.timeout:
            return False, 'no answer: timed out', self.timeout * 1000.0
        return True, 'home proxy pid 777', seconds * 1000.0

    def make_relay(self, port, choose, on_route_refused):
        self.relay = FakeRelay(self, port, choose, on_route_refused)
        return self.relay

    def drop_all(self, code=255):
        """Every route goes away: running ssh children end, new ones exit at once."""
        self.refuse = {d: code for d in set(self.started)}
        for c in self.children:
            if c.returncode is None:
                c.exit(code)


@pytest.fixture
def world(monkeypatch, inventory):
    w = World(Clock())
    monkeypatch.setattr(field.subprocess, 'Popen', w.popen)
    monkeypatch.setattr(field, 'port_open', w.listening)
    monkeypatch.setattr(field, 'make_relay', w.make_relay)
    write_inventory(inventory, TARGETS)
    w.inventory = inventory
    return w


def tunnel(w, **kw):
    block = {'machine': HOME_MACHINE, 'restart_delay_s': 5, 'check_interval_s': 10, 'attempt_timeout_s': 30,
             'unhealthy_restart_s': 60, 'stop_wait_s': 1}
    block.update(kw)
    t = Tunnel.from_block(block)
    t.clock = w.clock
    t.wall = lambda: WALL
    t.probe, t.executor, w.timeout = w.probe, (lambda fn: fn()), t.probe_timeout_s  # the probe seam, inline
    return t


def run(t, w, until, limit=50000, each=None):
    for _ in range(limit):
        if until():
            return
        delay = t.step()
        if each:
            each()             # after the step, at the step's time
        w.clock.advance(delay)
    raise AssertionError('not reached: phase=%s' % t.phase)


def run_for(t, w, seconds, each=None):
    end = w.clock() + seconds
    run(t, w, lambda: w.clock() >= end, each=each)


def cur(t):
    return t.current.dest if t.current is not None else None


def settled(t, w):
    """Started, every route measured once, the startup pick made."""
    t.start()
    run(t, w, lambda: not t.startup)


def lines(capsys, kind):
    return [x for x in capsys.readouterr().err.splitlines() if x.startswith('tunnel: %s ' % kind)]


def switches(err):
    return [x for x in err.splitlines() if x.startswith('tunnel: switch ')]


# ---------------------------------------------------------------------- parameters
def test_new_parameters_have_defaults_in_code_and_in_the_project_block():
    block = read_block(ROOT / 'claude_route.yaml', 'field')
    for key, value in NEW_PARAMS.items():
        assert DEFAULTS[key] == value, key
        assert block[key] == value, key


def test_project_block_values_reach_the_tunnel(tmp_path):
    t = Tunnel.from_block(read_block(ROOT / 'claude_route.yaml', 'field'))
    assert t.route_ports == (17900, 17915) and t.samples == 5 and t.probe_timeout_s == 5
    assert (t.window_s, t.measure_interval_s, t.sample_gap_s) == (600, 60, 1)
    assert (t.fail_penalty_ms, t.drop_penalty_ms) == (5000, 2000)
    assert (t.switch_margin_ratio, t.switch_margin_ms, t.switch_hold_s, t.switch_cooldown_s) == (0.3, 50, 120, 300)
    assert t.failover_after_fails == 3
    whole = yaml.safe_load((ROOT / 'claude_route.yaml').read_text())
    whole['field']['switch_hold_s'] = 45
    path = tmp_path / 'p.yaml'
    path.write_text(yaml.safe_dump(whole))
    assert Tunnel.from_block(read_block(path, 'field')).switch_hold_s == 45


@pytest.mark.parametrize('key,value', [
    ('probe_timeout_s', 0), ('probe_timeout_s', 'x'), ('samples', 0), ('samples', 1.5), ('samples', True),
    ('sample_gap_s', -1), ('measure_interval_s', 0), ('window_s', 0), ('fail_penalty_ms', -1),
    ('drop_penalty_ms', 'x'), ('switch_margin_ratio', 1.0), ('switch_margin_ratio', -0.1),
    ('switch_margin_ms', -1), ('switch_hold_s', -1), ('switch_cooldown_s', -1), ('failover_after_fails', 0),
    ('failover_after_fails', 2.5)])
def test_bad_new_parameters_are_refused(key, value):
    with pytest.raises(ConfigError, match=key):
        Tunnel.from_block({'machine': HOME_MACHINE, key: value})


@pytest.mark.parametrize('ports', [
    [17885, 17895],     # holds the reserved 17890
    [17890, 17890],     # is the reserved 17890
    [14000, 14002],     # holds the reserved 14001
    [17892, 17899],     # holds local_port
    [17910, 17900],     # reversed
    [17900],            # not a pair
    '17900-17915',
    [1000, 1010],       # below 1024
])
def test_bad_route_ports_are_refused(ports):
    with pytest.raises(ConfigError, match='route_ports'):
        Tunnel.from_block({'machine': HOME_MACHINE, 'route_ports': ports})


def test_more_routes_than_route_ports_is_a_config_error(inventory):
    write_inventory(inventory, TARGETS)
    with pytest.raises(ConfigError, match='route_ports'):
        Tunnel.from_block({'machine': HOME_MACHINE, 'route_ports': [17900, 17902]})


@pytest.mark.parametrize('ports', [[17885, 17895], [17892, 17899], [17900, 17901]])
def test_route_ports_errors_exit_78(tmp_path, inventory, ports, capsys):
    write_inventory(inventory, TARGETS)
    path = tmp_path / 'p.yaml'
    path.write_text(yaml.safe_dump({'field': {'machine': HOME_MACHINE, 'route_ports': ports}}))
    with pytest.raises(SystemExit) as exc:
        claude_route.main(Tunnel, 'field', ['--params', str(path)])
    assert exc.value.code == EXIT_CONFIG
    assert 'route_ports' in capsys.readouterr().err.strip().splitlines()[-1]


def test_default_route_ports_avoid_reserved_local_and_remote_ports():
    low, high = DEFAULTS['route_ports']
    span = set(range(low, high + 1))
    assert not span & set(RESERVED_PORTS)
    assert DEFAULTS['local_port'] not in span and DEFAULTS['remote_port'] not in span


def test_inventory_growing_past_route_ports_skips_the_extra_routes(world, capsys):
    """At start too many routes exit 78 (above); a longer inventory read while running only uses the first
    ones, in list order, and says once which route it skips."""
    world.latency = {d: 0.2 for d in TARGETS}
    t = tunnel(world, route_ports=[17900, 17903])
    settled(t, world)
    world.latency['x'] = 0.01
    write_inventory(world.inventory, ['x'] + TARGETS)    # x first: d no longer fits
    run_for(t, world, 250)
    assert t.fatal is None and [r.dest for r in t.routes] == ['x', 'a', 'b', 'c']
    assert not world.alive('d') and t.route('x').port == 17903       # d's port, the only free one
    err = capsys.readouterr().err
    assert err.count('tunnel: route d skipped: no free port in route_ports') == 1
    assert cur(t) == 'x'


def test_relay_refused_route_counts_a_failure_and_fails_over(world, capsys):
    """The relay's on_route_refused(port): one failed sample for that route; on the route in use,
    failover_after_fails of them in a row switch at once."""
    t = tunnel(world)
    settled(t, world)
    capsys.readouterr()
    port = t.route('a').port
    before = sum(1 for _, ok, _ in t.route('a').results if not ok)
    world.relay.on_route_refused(port)        # called from a relay thread; the next step takes it
    t.step()
    assert cur(t) == 'a' and sum(1 for _, ok, _ in t.route('a').results if not ok) == before + 1
    err = capsys.readouterr().err
    assert 'tunnel: a failed: relayed connection refused on port %d' % port in err
    for _ in range(3):
        world.relay.on_route_refused(port)
    t.step()
    assert cur(t) == 'b'
    assert switches(capsys.readouterr().err) == [
        'tunnel: switch a -> b: failover (3 failed probes on a: relayed connection refused on port %d)' % port]


def test_recovered_round_reselects_by_margin_only(world, capsys):
    """After a time without any route, the first route up is used at once; when the round is measured the best
    one is taken by the margin alone, without the hold or a cooldown."""
    world.latency = {'a': 0.9, 'b': 0.3, 'c': 0.9, 'd': 0.9}
    # every route dropped once in the outage; without the drop penalty the margin compares the speeds alone
    t = tunnel(world, switch_hold_s=3600, switch_cooldown_s=3600, drop_penalty_ms=0)
    settled(t, world)
    assert cur(t) == 'b'
    world.drop_all(255)
    run(t, world, lambda: t.round == 2)
    world.refuse = {}
    capsys.readouterr()
    run(t, world, lambda: t.current is not None and not t.settling)
    assert cur(t) == 'b'
    assert switches(capsys.readouterr().err) == ['tunnel: switch none -> a: recovered',
                                                 'tunnel: switch a -> b: recovered best (cost 300ms < 900ms)']
    assert '| switched %s from a: recovered best (cost 300ms < 900ms) |' % hms() in t.phase


def test_recovered_round_keeps_the_route_within_the_margin(world, capsys):
    world.latency = {'a': 0.4, 'b': 0.3, 'c': 0.9, 'd': 0.9}
    t = tunnel(world, drop_penalty_ms=0)
    settled(t, world)
    world.drop_all(255)
    run(t, world, lambda: t.round == 2)
    world.refuse = {}
    capsys.readouterr()
    run(t, world, lambda: t.current is not None and not t.settling)
    assert cur(t) == 'a' and switches(capsys.readouterr().err) == ['tunnel: switch none -> a: recovered']


def test_current_recovering_resets_the_hold_and_the_third_fail_in_a_row_is_a_failover(world, capsys):
    """Two failed checks, a good one (the count in a row starts again), then failures: the switch comes at the
    third failure in a row, as a failover, not at the third failure in all and not as a better route."""
    world.latency = {'a': 0.4, 'b': 0.3, 'c': 0.9, 'd': 0.9}
    t = tunnel(world, switch_hold_s=600)
    settled(t, world)
    assert cur(t) == 'a'
    t0 = world.clock()
    world.latency['a'] = [None, None, 0.4] + [None] * 10
    capsys.readouterr()
    run(t, world, lambda: cur(t) != 'a')
    failed = [p for p in world.probes if p[1] == 'a' and p[0] >= t0 and p[2] is None]
    assert len(failed) == 5 and t.switched_at == failed[-1][0]
    got = switches(capsys.readouterr().err)
    assert got == ['tunnel: switch a -> b: failover (3 failed probes on a: no answer: timed out)']


# ---------------------------------------------------------------------- measuring
def test_every_route_gets_its_own_loopback_forward_in_the_range(world):
    t = tunnel(world)
    settled(t, world)
    assert world.started == TARGETS
    forwards = [c.argv[c.argv.index('-L') + 1] for c in world.children]
    ports = [int(f.split(':')[1]) for f in forwards]
    assert ports == [17900, 17901, 17902, 17903]
    assert all(f.startswith('127.0.0.1:') and f.endswith(':127.0.0.1:17891') for f in forwards)
    assert all(c.argv[-1] in TARGETS for c in world.children)
    assert world.relay.port == 17892 and world.relay.listening


def test_startup_measures_every_route_samples_times(world):
    t = tunnel(world)
    settled(t, world)
    for dest in TARGETS:
        assert len([p for p in world.probes if p[1] == dest]) >= 5, dest


def test_unreachable_routes_do_not_hold_up_the_others(world, capsys):
    world.hang, world.refuse = {'b'}, {'c': 255}
    t = tunnel(world)
    t.start()
    t0 = world.clock()
    run(t, world, lambda: t.current is not None)
    assert cur(t) == 'a' and world.clock() - t0 < 1 and world.relay.listening
    done = []
    run(t, world, lambda: not t.startup, each=lambda: t.startup or done.append(world.clock()))
    assert done[0] - t0 == 30
    err = capsys.readouterr().err
    assert 'tunnel: c failed: ssh exited with 255' in err
    assert 'tunnel: b failed: no healthy forward within 30 s' in err


def test_current_route_every_check_interval_standby_every_measure_interval(world):
    t = tunnel(world)
    settled(t, world)
    world.probes.clear()
    run_for(t, world, 630)
    a = [p[0] for p in world.probes if p[1] == 'a']
    b = [p[0] for p in world.probes if p[1] == 'b']
    assert all(abs(y - x - 10) < 1e-6 for x, y in zip(a, a[1:]))
    starts = [b[0]] + [y for x, y in zip(b, b[1:]) if y - x > 1.5]
    assert all(abs(y - x - 60) < 1e-6 for x, y in zip(starts, starts[1:]))
    assert len(b) == 5 * len(starts)


def test_inventory_is_read_again_at_each_measure_cycle(world):
    world.latency = {d: 0.5 for d in TARGETS}
    t = tunnel(world)
    settled(t, world)
    world.latency['e'] = 0.05
    write_inventory(world.inventory, TARGETS + ['e'])
    run_for(t, world, 60 + 10)
    assert 'e' in world.started and t.route('e').port == 17904
    run_for(t, world, 400)
    assert cur(t) == 'e'
    write_inventory(world.inventory, ['a', 'b'])
    run_for(t, world, 70)
    assert cur(t) in ('a', 'b') and not world.alive('e') and not world.alive('c')


# ---------------------------------------------------------------------- the score
def test_fastest_route_wins_over_list_order(world):
    world.latency = {'a': 2.0, 'b': 0.3, 'c': 0.3, 'd': 0.5}
    t = tunnel(world)
    settled(t, world)
    assert cur(t) == 'b'


@pytest.mark.parametrize('order,first', [(['a', 'b', 'c', 'd'], 'a'), (['c', 'a', 'b', 'd'], 'c')])
def test_tie_is_broken_by_inventory_order(world, order, first):
    write_inventory(world.inventory, order)
    world.latency = {d: 0.3 for d in order}
    t = tunnel(world)
    settled(t, world)
    run_for(t, world, 900)
    assert cur(t) == first


def test_speed_is_the_median_not_one_spike(world):
    world.latency = {'a': [0.3, 0.3, 4.0, 0.3, 0.3], 'b': 0.5, 'c': 0.5, 'd': 0.5}
    t = tunnel(world)
    settled(t, world)
    run_for(t, world, 600)
    assert cur(t) == 'a'


def unstable(fast=0.2):
    return [fast, None]           # every other probe times out


def test_unstable_fast_route_loses_to_a_stable_slower_one(world):
    world.latency = {'a': unstable(), 'b': 0.4, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    run_for(t, world, 600)
    assert cur(t) == 'b'


def test_score_weights_are_parameters(world):
    world.latency = {'a': unstable(), 'b': 0.4, 'c': 0.9, 'd': 0.9}
    t = tunnel(world, fail_penalty_ms=0)
    settled(t, world)
    run_for(t, world, 600)
    assert cur(t) == 'a'


def test_drops_count_against_a_route_until_they_leave_the_window(world):
    world.latency = {'a': 0.1, 'b': 0.4, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    assert cur(t) == 'a'
    for _ in range(3):                    # a drops three times; it comes back at the next measure cycle
        world.alive('a')[0].exit(255)
        run(t, world, lambda: world.alive('a') and t.route('a').up)
    assert cur(t) == 'b'
    run_for(t, world, 400)
    assert cur(t) == 'b'                  # 100 ms + 3 drops x 2000 ms is not better than 400 ms
    run_for(t, world, 600 + 120 + 300)    # the drops leave the window; then the hold
    assert cur(t) == 'a'


def derp_world(world, order):
    """A measured outage: no LAN; Tailscale only through a DERP relay (ssh handshake 15 s, health about 10 s);
    the domestic VPS tunnel and the overseas relay tunnel answer. Only the Tailscale figures were measured in
    that outage; the others stand for any route that answers in well under a second."""
    write_inventory(world.inventory, order)
    world.refuse = {'lan': 255}
    world.connect_delay = {'ts': 15}
    world.latency = {'ts': 10.0, 'vps': 0.3, 'overseas': 0.9}


def test_derp_route_of_11_14_is_not_chosen(world, capsys):
    derp_world(world, ['lan', 'ts', 'vps', 'overseas'])
    t = tunnel(world)
    seen = set()
    t.start()
    run(t, world, lambda: world.clock() >= 1000 + 3600, each=lambda: seen.add(cur(t)))
    assert cur(t) == 'vps' and 'ts' not in seen
    measured = [MEASURE_RE.match(x) for x in lines(capsys, 'measure')]
    assert all(m for m in measured)
    ts = [m for m in measured if m.group(1) == 'ts']
    assert ts and all(m.group(9) == 'inf' for m in ts)
    assert 'last: no healthy forward within 30 s (no answer: timed out)' in ts[0].group(0)


def test_derp_route_answering_within_a_longer_timeout_still_loses(world, capsys):
    """Even with a probe timeout that lets the 10 s answer count, and 23 drops an hour, it is never used."""
    derp_world(world, ['lan', 'ts', 'vps', 'overseas'])
    t = tunnel(world, probe_timeout_s=15)
    seen = set()
    drops = iter(1000 + 30 + 156.5 * i for i in range(24))
    nxt = [next(drops)]

    def each():
        seen.add(cur(t))
        if world.clock() >= nxt[0]:
            for c in world.alive('ts'):
                c.exit(255)
            nxt[0] = next(drops, float('inf'))

    t.start()
    run(t, world, lambda: world.clock() >= 1000 + 3600, each=each)
    assert cur(t) == 'vps' and 'ts' not in seen
    costs = [m.group(9) for m in map(MEASURE_RE.match, lines(capsys, 'measure')) if m and m.group(1) == 'ts']
    assert costs and all(c == 'inf' or int(c[:-2]) >= 10000 for c in costs)


def test_late_tailscale_does_not_leave_the_slower_overseas_route_in_use(world):
    derp_world(world, ['lan', 'ts', 'overseas', 'vps'])
    world.hang = {'ts'}                   # not up within attempt_timeout_s
    t = tunnel(world)
    settled(t, world)
    assert cur(t) == 'vps'


# ---------------------------------------------------------------------- the switch gate
def test_better_by_less_than_the_ratio_does_not_switch(world, capsys):
    world.latency = {'a': 0.5, 'b': 0.4, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    run_for(t, world, 1200)
    assert cur(t) == 'a'
    assert switches(capsys.readouterr().err) == ['tunnel: switch none -> a: startup']


def test_better_by_the_ratio_but_less_than_margin_ms_does_not_switch(world, capsys):
    world.latency = {'a': 0.05, 'b': 0.01, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    run_for(t, world, 1200)
    assert cur(t) == 'a'
    assert switches(capsys.readouterr().err) == ['tunnel: switch none -> a: startup']


def test_better_for_less_than_the_hold_does_not_switch(world, capsys):
    world.latency = {'a': 0.9, 'b': 0.9, 'c': 0.9, 'd': 0.9}
    t = tunnel(world, switch_hold_s=300)
    settled(t, world)
    t0 = world.clock()
    world.latency['b'] = lambda now: 0.3 if now < t0 + 200 else 0.9
    run_for(t, world, 1200)
    assert cur(t) == 'a'
    assert switches(capsys.readouterr().err) == ['tunnel: switch none -> a: startup']


def test_better_for_the_hold_switches_once(world, capsys):
    world.latency = {'a': 0.9, 'b': 0.9, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    world.latency['b'] = 0.3
    run(t, world, lambda: cur(t) == 'b')
    first_better = min(p[0] for p in world.probes if p[1] == 'b' and p[2] == 0.3)
    assert 120 <= world.clock() - first_better <= 120 + 60 + 5
    run_for(t, world, 1200)
    got = switches(capsys.readouterr().err)
    assert got[1:] == ['tunnel: switch a -> b: better (cost 300ms < 900ms for 120 s)']


def test_no_voluntary_switch_during_the_cooldown(world, capsys):
    world.latency = {'a': 0.9, 'b': 0.9, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    world.latency['b'] = 0.5
    run(t, world, lambda: cur(t) == 'b')
    switched = world.clock()
    world.latency['c'] = 0.1
    run(t, world, lambda: cur(t) == 'c')
    assert world.clock() - switched >= 300


def test_alternating_latencies_do_not_flap(world, capsys):
    world.latency = {'a': lambda now: 0.3 if int(now // 60) % 2 else 0.6,
                     'b': lambda now: 0.6 if int(now // 60) % 2 else 0.3, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    capsys.readouterr()
    run_for(t, world, 1800)
    assert len(switches(capsys.readouterr().err)) <= 1


# ---------------------------------------------------------------------- forced switches
def in_cooldown(world, t):
    """a is current, then a better b takes over: the cooldown runs."""
    world.latency = {'a': 0.9, 'b': 0.9, 'c': 0.9, 'd': 0.9}
    settled(t, world)
    world.latency['b'] = 0.3
    run(t, world, lambda: cur(t) == 'b')
    world.latency['c'] = 0.4
    run_for(t, world, 65)                 # c measured again


def test_drop_of_the_current_route_switches_at_once_even_in_the_cooldown(world, capsys):
    t = tunnel(world)
    in_cooldown(world, t)
    capsys.readouterr()
    world.alive('b')[0].exit(0)
    w0 = world.clock()
    open_ = []
    run(t, world, lambda: cur(t) != 'b', each=lambda: open_.append(world.relay.listening))
    assert cur(t) == 'c' and t.switched_at == w0 and all(open_)
    assert switches(capsys.readouterr().err) == ['tunnel: switch b -> c: failover (b dropped: ssh exited with 0)']


def test_failed_probes_switch_after_failover_after_fails_even_in_the_cooldown(world, capsys):
    t = tunnel(world)
    in_cooldown(world, t)
    capsys.readouterr()
    world.latency['b'] = None
    w0 = world.clock()
    run(t, world, lambda: cur(t) != 'b', each=lambda: world.relay.listening or pytest.fail('port closed'))
    assert cur(t) == 'c' and t.switched_at - w0 <= 3 * 10 < 60
    got = switches(capsys.readouterr().err)
    assert got == ['tunnel: switch b -> c: failover (3 failed probes on b: no answer: timed out)']


def test_single_route_one_probe_timeout_keeps_the_port_open_and_counts_no_round(world, capsys):
    write_inventory(world.inventory, ['a'])
    t = tunnel(world)
    settled(t, world)
    capsys.readouterr()
    t0 = world.clock()
    world.latency['a'] = lambda now: None if t0 + 5 <= now < t0 + 15 else 0.05
    open_ = []
    run_for(t, world, 120, each=lambda: open_.append((world.relay.listening, t.round)))
    assert all(o == (True, 1) for o in open_) and cur(t) == 'a'
    err = capsys.readouterr().err
    assert 'dropped' not in err and 'round' not in err and 'switch' not in err
    assert re.search(r'tunnel: measure a port 17900 ok 0/1 median \d+ms fail 1/\d+ drops 0 cost \d+ms current '
                     r'last: no answer: timed out', err)


def test_single_route_failing_probes_short_of_unhealthy_restart_s_keep_the_port_open(world, capsys):
    write_inventory(world.inventory, ['a'])
    t = tunnel(world)
    settled(t, world)
    t0 = world.clock()
    world.latency['a'] = lambda now: None if now < t0 + 55 else 0.05
    open_ = []
    run_for(t, world, 120, each=lambda: open_.append(world.relay.listening))
    assert all(open_) and cur(t) == 'a' and t.round == 1 and world.started == ['a']


def test_single_route_failing_for_unhealthy_restart_s_is_a_drop_and_closes_the_port(world, capsys):
    write_inventory(world.inventory, ['a'])
    t = tunnel(world)
    settled(t, world)
    world.latency['a'] = None
    run(t, world, lambda: t.current is None)
    assert not world.relay.listening and 'tunnel: a dropped: unhealthy for 60 s' in capsys.readouterr().err


# ---------------------------------------------------------------------- no route at all
def test_all_routes_down_close_the_port_and_exit_78_after_max_failed_rounds(world, capsys):
    t = tunnel(world, max_failed_rounds=3)
    settled(t, world)
    world.drop_all(255)
    closed = []
    with pytest.raises(SystemExit) as exc:
        run(t, world, lambda: False, each=lambda: closed.append(not world.relay.listening))
    t.close()
    assert exc.value.code == EXIT_CONFIG and all(closed)
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last.startswith('tunnel: no ssh target of machine %s connects in 3 rounds in a row (last round, '
                           'tried in order: a: ssh exited with 255; b: ssh exited with 255; c: ' % HOME_MACHINE)
    assert t.phase.startswith('no route:') and world.relay.shut


def test_a_route_coming_back_reopens_the_port_and_resets_the_rounds(world, capsys):
    t = tunnel(world, max_failed_rounds=5)
    settled(t, world)
    world.drop_all(255)
    run(t, world, lambda: t.round == 3)
    assert not world.relay.listening
    world.refuse = {'a': 255, 'b': 255, 'd': 255}
    run(t, world, lambda: t.current is not None)
    assert cur(t) == 'c' and world.relay.listening and t.round == 1
    assert world.relay.new_connection_port() == t.route('c').port
    assert 'tunnel: switch none -> c: recovered' in capsys.readouterr().err


# ---------------------------------------------------------------------- new connections follow the pick
def test_new_connections_go_to_the_current_route_then_the_next_candidate(world):
    world.latency = {'a': 2.0, 'b': 0.3, 'c': 0.5, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    route = {d: t.route(d).port for d in TARGETS}
    assert world.relay.choose() == [route['b'], route['c'], route['d'], route['a']]


def test_start_probes_local_port_first_and_a_taken_port_starts_no_ssh(world, monkeypatch, capsys):
    """start() probes local_port before anything else; another program on it: no ssh is started and the
    first step exits 78 (start() itself returns, as test_port_used_by_another_program_exits_78 expects)."""
    asked = []

    def listening(port=None, **_):
        asked.append(port)
        return port == 17892              # another program accepts on local_port

    monkeypatch.setattr(field, 'port_open', listening)
    t = tunnel(world)
    t.start()
    assert asked == [17892] and world.children == []
    with pytest.raises(SystemExit) as exc:
        t.step()
    assert exc.value.code == EXIT_CONFIG and world.children == []
    assert capsys.readouterr().err.strip().splitlines()[-1] == (
        'tunnel: 127.0.0.1:17892 is already in use by another program; set field.local_port to a free port')


def test_bound_local_port_exits_78(world, capsys):
    world.port_taken = True
    t = tunnel(world)
    t.start()
    with pytest.raises(SystemExit) as exc:
        run(t, world, lambda: False)
    assert exc.value.code == EXIT_CONFIG
    assert 'already in use by another program' in capsys.readouterr().err


# ---------------------------------------------------------------------- what the user sees
def hms():
    return time.strftime('%H:%M:%S', time.localtime(WALL))


def test_phase_names_the_route_its_latency_and_the_last_switch(world):
    world.latency = {'a': 2.0, 'b': 0.3, 'c': 0.9}
    world.refuse = {'d': 255}
    t = tunnel(world)
    settled(t, world)
    assert t.phase == 'up: via b 300ms (fail 0/5, drops 0) | switched never | standby: a 2000ms, c 900ms, d down'


def test_phase_names_the_time_source_and_reason_of_a_better_switch(world):
    world.latency = {'a': 0.9, 'b': 0.9, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    assert '| switched never |' in t.phase
    world.latency['b'] = 0.3
    run(t, world, lambda: cur(t) == 'b')
    assert '| switched %s from a: better (cost 300ms < 900ms for 120 s) |' % hms() in t.phase


def test_phase_after_a_drop_names_the_failover(world):
    t = tunnel(world)
    settled(t, world)
    world.alive('a')[0].exit(0)
    run(t, world, lambda: cur(t) != 'a')
    assert t.phase.startswith('up: via b 50ms (fail 0/')
    assert '| switched %s from a: failover (a dropped: ssh exited with 0) |' % hms() in t.phase


def test_every_measurement_logs_one_line(world, capsys):
    world.refuse = {'d': 255}
    t = tunnel(world)
    settled(t, world)
    got = lines(capsys, 'measure')
    assert sorted(MEASURE_RE.match(x).group(1) for x in got) == ['a', 'b', 'c', 'd']
    assert [MEASURE_RE.match(x).group(9) for x in got if ' d ' in x] == ['inf']
    got = [x for x in got if ' d ' not in x]
    assert MEASURE_RE.match(got[0]).groups()[:9] == ('a', '17900', '5', '5', '50ms', '0', '5', '0', '50ms')
    run_for(t, world, 60 + 5 + 0.5)
    got = lines(capsys, 'measure')
    assert [MEASURE_RE.match(x).group(1) for x in got].count('a') in (6, 7)  # current: every check_interval_s
    assert [MEASURE_RE.match(x).group(1) for x in got].count('b') == 1       # standby: one burst
    assert all(MEASURE_RE.match(x) for x in got)


def test_every_switch_logs_one_line(world, capsys):
    world.latency = {'a': 0.9, 'b': 0.9, 'c': 0.9, 'd': 0.9}
    t = tunnel(world)
    settled(t, world)
    world.latency['b'] = 0.3
    run(t, world, lambda: cur(t) == 'b')
    world.alive('b')[0].exit(0)
    run(t, world, lambda: cur(t) != 'b')
    assert switches(capsys.readouterr().err) == [
        'tunnel: switch none -> a: startup',
        'tunnel: switch a -> b: better (cost 300ms < 900ms for 120 s)',
        'tunnel: switch b -> a: failover (b dropped: ssh exited with 0)']
