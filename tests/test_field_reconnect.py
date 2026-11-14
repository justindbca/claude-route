"""applet-tunnel-1 after a drop: the three exits of the field log, and the in-process reconnect.

Fake ssh (a ``subprocess.Popen`` stand-in) and a fake clock; no process, socket or sleep. ``Net`` is the world
the fake ssh sees: which destinations connect, whether the home proxy answers through the forward, and so
whether ``127.0.0.1:<local_port>`` is listening (only a connected ssh child listens).

Every route has its own ssh child on its own route port; ``127.0.0.1:<local_port>`` is the relay's (a
stand-in here), open only while a route is in use. The probe executor is injected and runs inline.

OpenSSH's own exit codes are kept: with ``-N`` a SIGTERM in the client loop (connected) ends ssh with 0; a
SIGTERM before the client loop (still connecting) kills it with the default action, -15.
"""
import itertools
import re
import signal

import pytest

from claude_route import EXIT_CONFIG, ConfigError, read_block
from claude_route import field
from claude_route.field import DEFAULTS, Tunnel
from conftest import HOME_MACHINE, write_inventory
from test_field import ROOT

TARGETS = ['a', 'b', 'c', 'd']


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class Net:
    """``refuse``: destination -> exit code of an ssh that ends by itself before connecting (255 = no route,
    -15 = killed). ``hang``: destinations whose ssh never connects (the attempt times out). ``answer``: the home
    proxy answers through a connected forward (False: the forward is open, the request times out)."""

    def __init__(self, clock):
        self.refuse, self.hang, self.answer = {}, set(), True
        self.children, self.pids, self.clock = [], itertools.count(4100), clock
        self.relay = None

    def popen(self, argv, **_):
        child = FakeSsh(self, argv)
        self.children.append(child)
        return child

    @property
    def started(self):
        return [c.dest for c in self.children]

    def current(self):
        alive = [c for c in self.children if c.poll() is None]
        return alive[-1] if alive else None

    def drop_all(self, code=0):
        """Every running ssh child ends by itself (the network went away)."""
        for c in self.children:
            if c.poll() is None:
                c.exit(code)

    def on_port(self, port):
        return next((c for c in self.children if c.port == port and c.poll() is None and c.connected), None)

    def listening(self, port=None, **_):
        """No port, or local_port: whether the relay listens. A route port: whether its ssh forwards."""
        if port is None or (self.relay is not None and port == self.relay.port):
            return self.relay is not None and self.relay.listening
        return self.on_port(port) is not None

    def make_relay(self, port, choose, on_route_refused=None):
        self.relay = Relay(port)
        return self.relay

    def health(self, port, **_):
        child = self.on_port(port)
        if child is None or not child.connected:
            return False, 'no answer: [Errno 61] Connection refused'
        if not self.answer:
            return False, 'no answer: timed out'
        return True, {'service': 'claude-route-homeproxy', 'pid': 777}


class Relay:
    def __init__(self, port):
        self.port, self.listening = port, False

    def open(self):
        self.listening = True

    def close(self):
        self.listening = False

    shutdown = close


class FakeSsh:
    def __init__(self, net, argv):
        self.net, self.argv, self.dest = net, list(argv), argv[-1]
        self.port = int(argv[argv.index('-L') + 1].split(':')[1])
        self.pid, self.started_at = next(net.pids), net.clock()
        self.returncode = None
        self.connected = self.dest not in net.refuse and self.dest not in net.hang

    def poll(self):
        if self.returncode is None and not self.connected and self.dest in self.net.refuse:
            self.returncode = self.net.refuse[self.dest]
        return self.returncode

    def exit(self, code):
        """ssh ends by itself (the server closed the connection, ServerAlive gave up, ...)."""
        self.returncode, self.connected = code, False

    def send_signal(self, sig):
        assert sig == signal.SIGTERM
        if self.returncode is None:
            self.returncode = 0 if self.connected else -signal.SIGTERM
        self.connected = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self.returncode = -signal.SIGKILL
        self.connected = False


@pytest.fixture
def world(monkeypatch, inventory):
    clock = Clock()
    net = Net(clock)
    monkeypatch.setattr(field.subprocess, 'Popen', net.popen)
    monkeypatch.setattr(field, 'health_ok', net.health)
    monkeypatch.setattr(field, 'port_open', net.listening)
    monkeypatch.setattr(field, 'make_relay', net.make_relay)
    write_inventory(inventory, TARGETS)
    return net, clock


def tunnel(clock, **kw):
    block = {'machine': HOME_MACHINE, 'restart_delay_s': 5, 'check_interval_s': 10, 'attempt_timeout_s': 30,
             'unhealthy_restart_s': 60, 'stop_wait_s': 1}
    block.update(kw)
    t = Tunnel.from_block(block)
    t.clock = clock
    t.executor = lambda fn: fn()          # probes run inline in step(): no thread, no timing
    return t


def run(t, clock, until, limit=2000):
    """Step the tunnel, advancing the fake clock by each returned delay, until ``until()``."""
    for _ in range(limit):
        if until():
            return
        clock.advance(t.step())
    raise AssertionError('not reached: phase=%s' % t.phase)


def sdk_run(t, clock, stop_when):
    """What _main_applet does: start, step until the SDK's ``stopping`` is set (SIGTERM from the host or
    host.draining makes app.run return), then close() in the finally block."""
    t.start()
    try:
        run(t, clock, stop_when)
    finally:
        t.close()


def tunnel_lines(err):
    return [line for line in err.splitlines() if line.startswith('tunnel: ')]


def shape(line):
    """A log line without its variable parts (ssh argv, pids)."""
    for prefix in ('tunnel: start (target', 'tunnel: up via', 'tunnel: ssh pid', 'tunnel: 127.0.0.1:',
                   'tunnel: measure'):
        if line.startswith(prefix):
            head = {'tunnel: start (target': line.split(')')[0] + ')',
                    'tunnel: up via': ' '.join(line.split()[:4]).rstrip(','),
                    'tunnel: ssh pid': 'tunnel: ssh pid stopped ' + line.split()[-1],
                    'tunnel: 127.0.0.1:': 'tunnel: closed; exits',
                    'tunnel: measure': ' '.join(line.split()[:3])}[prefix]
            return head
    return line


# ---------------------------------------------------------------------- the three exits of the field log
# Each one ends through close() after the main loop stopped (the SDK's `stopping`: SIGTERM or host.draining).
# None of them is the tunnel's own exit: that one always logs "dropped" / "failed" lines and a last
# "no ssh target ... connects" line before it, which the field log does not have.
OWN_EXIT_LINE = re.compile(r'tunnel: (\S+ dropped:|\S+ failed:|no ssh target|round \d)')


STARTS = ['tunnel: start (target %d/4)' % i for i in range(1, 5)]
UPS = ['tunnel: up via a', 'tunnel: switch none -> a: startup', 'tunnel: up via b', 'tunnel: up via c',
       'tunnel: up via d']


def test_log_exit_1_up_then_health_timeout_then_stopped_0(world, capsys):
    net, clock = world
    t = tunnel(clock)
    stop = {'now': False}

    def stop_when():
        if t.healthy and net.answer:
            net.answer = False            # asleep: the forwards are open, the requests through them time out
        elif t.current is not None and t.current.fail_since is not None and t.current.burst_left == 0:
            stop['now'] = True            # the host's SIGTERM arrives after the first measurement with failures
        return stop['now']

    sdk_run(t, clock, stop_when)
    raw = tunnel_lines(capsys.readouterr().err)
    got = [shape(x) for x in raw]
    assert got == STARTS + UPS + ['tunnel: measure a', 'tunnel: measure b', 'tunnel: measure c',
                                  'tunnel: measure d'] + ['tunnel: ssh pid stopped (0)'] * 4 + ['tunnel: closed; exits']
    assert [x for x in raw if x.startswith('tunnel: measure a ')][0].endswith(' current last: no answer: timed out')
    assert not any(OWN_EXIT_LINE.match(line) for line in got)


def test_log_exit_2_up_then_stopped_0(world, capsys):
    net, clock = world
    t = tunnel(clock)
    sdk_run(t, clock, lambda: t.healthy)
    got = [shape(x) for x in tunnel_lines(capsys.readouterr().err)]
    assert got == STARTS + UPS + ['tunnel: ssh pid stopped (0)'] * 4 + ['tunnel: closed; exits']


def test_log_exit_3_start_then_stopped_minus_15(world, capsys):
    net, clock = world
    net.hang = set(TARGETS)               # just woke up: every ssh is still connecting
    t = tunnel(clock)
    sdk_run(t, clock, lambda: t.child is not None)
    got = [shape(x) for x in tunnel_lines(capsys.readouterr().err)]
    assert got == STARTS + ['tunnel: ssh pid stopped (-15)'] * 4 + ['tunnel: closed; exits']
    assert not net.listening()


# ---------------------------------------------------------------------- reconnect in the process
def waiting(t, n):
    """Round ``n`` is next and no ssh child runs (between rounds)."""
    return getattr(t, 'round', None) == n and t.child is None


def up_on(t, clock, dest):
    run(t, clock, lambda: t.healthy and t.dest == dest)


def test_health_timeout_after_up_reconnects_in_process(world):
    """Woken up: health times out on every route until each one is a drop, then the network is still down for
    a whole round; no exit."""
    net, clock = world
    t = tunnel(clock)
    t.start()
    up_on(t, clock, 'a')
    net.answer = False
    net.refuse = {'a': 255, 'b': 255, 'd': 255}
    net.hang = {'c'}                       # a connect timeout is one more failed target, not an exit
    run(t, clock, lambda: waiting(t, 2))
    assert net.started[:4] == TARGETS and net.started[-4:] == TARGETS  # round 1: every target, in list order
    assert not net.listening() and t.phase.startswith('reconnecting: round 2/10')
    net.answer, net.refuse, net.hang = True, {}, set()
    before = len(net.started)
    up_on(t, clock, 'a')
    assert net.started[before:] == TARGETS and t.round == 1


def test_ssh_exit_0_after_up_reconnects_from_the_target_that_dropped(world):
    net, clock = world
    net.refuse = {'a': 255}
    t = tunnel(clock)
    t.start()
    up_on(t, clock, 'b')
    net.refuse = {d: 255 for d in TARGETS}
    net.drop_all(0)                        # with another route up, a drop of b alone would switch to it
    run(t, clock, lambda: waiting(t, 2))
    assert net.started == TARGETS * 2      # round 1 starts every target again, in list order
    net.refuse = {'b': 255}
    up_on(t, clock, 'a')                   # equal routes: the first in list order
    assert net.started[8:] == TARGETS


def test_ssh_minus_15_while_connecting_tries_further_rounds(world):
    """The start phase: every ssh dies with -15 in round 1; the applet waits and tries a second round."""
    net, clock = world
    net.refuse = {d: -15 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 2))
    assert net.started == TARGETS
    net.refuse = {}
    run(t, clock, lambda: t.child is not None)
    assert net.started == TARGETS * 2
    assert net.children[4].started_at - net.children[3].started_at >= 5  # restart_delay_s between rounds
    up_on(t, clock, 'a')


def test_phase_names_the_round_and_the_target(world):
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock, max_failed_rounds=3)
    t.start()
    run(t, clock, lambda: waiting(t, 2))
    assert t.phase == 'reconnecting: round 2/3 in 5 s; round 1: no target connects'
    seen = []
    run(t, clock, lambda: seen.append(t.phase) or t.child is not None)
    assert seen[-1] == ('reconnecting: round 2/3, trying a (target 1/4), b (target 2/4), c (target 3/4), '
                        'd (target 4/4)')


def test_port_stays_closed_while_reconnecting(world):
    net, clock = world
    t = tunnel(clock)
    t.start()
    up_on(t, clock, 'a')
    net.refuse = {d: 255 for d in TARGETS}
    net.drop_all(0)
    closed = []
    run(t, clock, lambda: closed.append(not net.listening()) or waiting(t, 3))
    assert all(closed[1:]) and t.child is None   # [0]: before the step that sees the drop


def test_n_failed_rounds_in_a_row_exit_78(world, capsys):
    net, clock = world
    net.refuse = {'a': 255}
    t = tunnel(clock, max_failed_rounds=3)
    t.start()
    up_on(t, clock, 'b')
    net.refuse = {d: 255 for d in TARGETS}
    net.drop_all(0)
    with pytest.raises(SystemExit) as exc:
        run(t, clock, lambda: False)
    t.close()
    assert exc.value.code == EXIT_CONFIG
    assert net.started == TARGETS + TARGETS * 3
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last.startswith('tunnel: no ssh target of machine %s connects in 3 rounds in a row (last round, '
                           'tried in order: a: ssh exited with 255; b: ' % HOME_MACHINE)
    assert t.phase.startswith('no route:') and not net.listening()


def test_a_good_round_resets_the_count(world):
    net, clock = world
    t = tunnel(clock, max_failed_rounds=2)
    t.start()
    up_on(t, clock, 'a')
    for _ in range(3):  # each loss of every route gets its own 2 rounds
        net.refuse = {d: 255 for d in TARGETS}
        net.drop_all(0)
        run(t, clock, lambda: waiting(t, 2))
        net.refuse = {}
        up_on(t, clock, 'a')
    assert t.round == 1


def test_sigterm_while_waiting_for_the_next_round_exits_cleanly(world, capsys):
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    sdk_run(t, clock, lambda: waiting(t, 2))
    assert t.fatal is None and not net.listening()
    assert tunnel_lines(capsys.readouterr().err)[-1].endswith('exits')


def test_every_start_is_an_inventory_target_through_the_forward(world):
    """No other route: each ssh is the -L forward of an inventory target on its route port, in every round."""
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock, max_failed_rounds=3)
    t.start()
    with pytest.raises(SystemExit):
        run(t, clock, lambda: False)
    assert net.started == TARGETS * 3
    assert all(c.argv[-3:-1] == ['-L', '127.0.0.1:%d:127.0.0.1:17891' % (17900 + TARGETS.index(c.dest))]
               for c in net.children)


# ---------------------------------------------------------------------- the parameter
def test_max_failed_rounds_default_and_project_block():
    assert DEFAULTS['max_failed_rounds'] == 10
    assert read_block(ROOT / 'claude_route.yaml', 'field')['max_failed_rounds'] == 10


@pytest.mark.parametrize('value', [0, -1, 1.5, '3', True, 10001])
def test_bad_max_failed_rounds_is_refused(value):
    with pytest.raises(ConfigError, match='max_failed_rounds'):
        Tunnel.from_block({'machine': HOME_MACHINE, 'max_failed_rounds': value})
