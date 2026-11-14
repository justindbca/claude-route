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
import subprocess

import pytest

from claude_route import EXIT_CONFIG, ConfigError, read_block
from claude_route import field
from claude_route.field import DEFAULTS, Tunnel
from conftest import HOME_MACHINE, write_inventory
from test_field import ROOT

TARGETS = ['a', 'b', 'c', 'd']


class Clock:
    """The monotonic clock (``now``, called) and the wall clock (``wall()``). ``sleep()`` is the machine asleep:
    the wall clock goes on, the monotonic clock and the process stand still (macOS, Linux)."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)

    def sleep(self, seconds):
        self.slept += float(seconds)

    def wall(self):
        return 1.7e9 + self.now + self.slept


class Net:
    """``refuse``: destination -> exit code of an ssh that ends by itself before connecting (255 = no route,
    -15 = killed). ``hang``: destinations whose ssh never connects (the attempt times out). ``answer``: the home
    proxy answers through a connected forward (False: the forward is open, the request times out)."""

    def __init__(self, clock):
        self.refuse, self.hang, self.answer = {}, set(), True
        self.children, self.pids, self.clock = [], itertools.count(4100), clock
        self.relay = None
        self.sig = ('198.51.100.7', None)   # network_signature(): the source addresses of the default routes
        self.stale = set()                 # ssh children whose connection died while the machine slept
        self.stubborn = set()              # pids of ssh children that neither SIGTERM nor SIGKILL ends (yet)
        self.timeouts = []                 # the timeout of each health request

    def signature(self):
        return self.sig

    def freeze(self):
        """Asleep long enough: every running ssh still runs, but its connection is dead (requests time out)."""
        self.stale |= {c.pid for c in self.children if c.poll() is None}

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

    def health(self, port, timeout=None, **_):
        self.timeouts.append(timeout)
        child = self.on_port(port)
        if child is None or not child.connected:
            return False, 'no answer: [Errno 61] Connection refused'
        if not self.answer or child.pid in self.stale:
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
        if self.pid in self.net.stubborn:
            return
        if self.returncode is None:
            self.returncode = 0 if self.connected else -signal.SIGTERM
        self.connected = False

    def wait(self, timeout=None):
        if self.returncode is None and self.pid in self.net.stubborn:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def kill(self):
        if self.pid in self.net.stubborn:
            return
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
    monkeypatch.setattr(field, 'network_signature', net.signature, raising=False)
    write_inventory(inventory, TARGETS)
    return net, clock


def tunnel(clock, **kw):
    block = {'machine': HOME_MACHINE, 'restart_delay_s': 5, 'check_interval_s': 10, 'attempt_timeout_s': 30,
             'unhealthy_restart_s': 60, 'stop_wait_s': 1}
    block.update(kw)
    t = Tunnel.from_block(block)
    t.clock, t.wall = clock, clock.wall
    t.executor = lambda fn: fn()          # probes run inline in step(): no thread, no timing
    return t


def run(t, clock, until, limit=20000):
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
    assert not net.listening() and t.phase.startswith('reconnecting: round 2 in ')
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
    assert t.phase == ('reconnecting: round 2/3 in 5 s; round 1: no target connects (a: ssh exited with 255; '
                       'b: ssh exited with 255; c: ssh exited with 255; d: ssh exited with 255)')
    seen = []
    run(t, clock, lambda: seen.append(t.phase) or t.child is not None)
    assert seen[-1] == ('reconnecting: round 2/3, trying a (target 1/4), b (target 2/4), c (target 3/4), '
                        'd (target 4/4); round 1: no target connects (a: ssh exited with 255; '
                        'b: ssh exited with 255; c: ssh exited with 255; d: ssh exited with 255)')


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
    """0 = no limit, the default: a field machine without network (lid closed, no Wi-Fi) keeps reconnecting."""
    assert DEFAULTS['max_failed_rounds'] == 0
    assert read_block(ROOT / 'claude_route.yaml', 'field')['max_failed_rounds'] == 0
    assert Tunnel.from_block({'machine': HOME_MACHINE, 'max_failed_rounds': 0}).max_failed_rounds == 0


@pytest.mark.parametrize('value', [-1, 1.5, '3', True, 10001])
def test_bad_max_failed_rounds_is_refused(value):
    with pytest.raises(ConfigError, match='max_failed_rounds'):
        Tunnel.from_block({'machine': HOME_MACHINE, 'max_failed_rounds': value})


@pytest.mark.parametrize('value', [2, 0, 3601, '60', True])
def test_bad_restart_delay_max_s_is_refused(value):
    """Out of 0.1-3600, not a number, or below restart_delay_s (5 here): a config error (exit 78)."""
    with pytest.raises(ConfigError, match='restart_delay_max_s'):
        Tunnel.from_block({'machine': HOME_MACHINE, 'restart_delay_s': 5, 'restart_delay_max_s': value})


def test_restart_delay_max_s_default_and_project_block():
    assert DEFAULTS['restart_delay_max_s'] == 60
    assert read_block(ROOT / 'claude_route.yaml', 'field')['restart_delay_max_s'] == 60


# ---------------------------------------------------------------------- never give up on the network
def lose_every_route(t, clock, net):
    """Up on a, then the network goes away: every ssh ends and none connects again."""
    t.start()
    up_on(t, clock, 'a')
    net.refuse = {d: 255 for d in TARGETS}
    net.drop_all(0)


def test_incident_far_more_than_10_failed_rounds_keep_the_process_and_the_closed_port(world):
    """The field machine sleeps all night and wakes up every 15 minutes without network: with the defaults the
    applet runs round after round (no exit 78), the port stays closed, and the first route that connects again
    opens it."""
    net, clock = world
    t = tunnel(clock)                       # max_failed_rounds: the default
    lose_every_route(t, clock, net)
    closed = []
    run(t, clock, lambda: closed.append(not net.listening()) or t.round == 60)
    assert all(closed[1:]) and t.current is None and t.fatal is None
    assert net.started.count('a') >= 60
    assert re.match(r'reconnecting: round 60( in |, trying )', t.phase), t.phase
    net.refuse = {'a': 255, 'b': 255, 'd': 255}
    up_on(t, clock, 'c')
    assert net.listening() and t.round == 1 and t.fatal is None


def test_never_connected_since_the_start_keeps_trying(world):
    """Started while there is no network at all (woken without Wi-Fi): no exit, the port opens once a route
    connects."""
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 40))
    assert not net.listening() and t.fatal is None
    net.refuse = {}
    up_on(t, clock, 'a')
    assert net.listening() and t.round == 1


def record_waits(t, clock, count):
    """Step until ``count`` waits before a round were set; each wait is next_round_at minus the step's time."""
    waits, seen = [], None
    for _ in range(20000):
        if len(waits) == count:
            return waits
        now = clock.now
        delay = t.step()
        if t.next_round_at is not None and t.next_round_at != seen:
            seen = t.next_round_at
            waits.append(round(seen - now, 3))
        clock.advance(delay)
    raise AssertionError('not reached: %s' % waits)


def test_round_wait_doubles_from_restart_delay_to_the_cap_and_starts_over_after_a_route_is_up(world):
    net, clock = world
    t = tunnel(clock)                       # restart_delay_s 5, restart_delay_max_s: the default (60)
    lose_every_route(t, clock, net)
    # before round 1, then after each failed round 1..8
    assert record_waits(t, clock, 9) == [5, 5, 10, 20, 40, 60, 60, 60, 60]
    net.refuse = {}
    up_on(t, clock, 'a')
    net.refuse = {d: 255 for d in TARGETS}
    net.drop_all(0)
    assert record_waits(t, clock, 4) == [5, 5, 10, 20]


def test_round_wait_cap_is_a_parameter(world):
    net, clock = world
    t = tunnel(clock, restart_delay_s=2, restart_delay_max_s=7)
    lose_every_route(t, clock, net)
    assert record_waits(t, clock, 6) == [2, 2, 4, 7, 7, 7]


# ---------------------------------------------------------------------- phase without a limit
NO_ROUTE_1 = ('round 1: no target connects (a: ssh exited with 255; b: ssh exited with 255; '
              'c: ssh exited with 255; d: ssh exited with 255)')


def test_phase_without_a_limit_names_the_round_the_wait_and_each_reason(world):
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    seen = []
    run(t, clock, lambda: seen.append(t.phase) or waiting(t, 2))
    assert t.phase == 'reconnecting: round 2 in 5 s; ' + NO_ROUTE_1
    run(t, clock, lambda: seen.append(t.phase) or t.child is not None)
    assert seen[-1] == ('reconnecting: round 2, trying a (target 1/4), b (target 2/4), c (target 3/4), '
                        'd (target 4/4); ' + NO_ROUTE_1)
    run(t, clock, lambda: seen.append(t.phase) or waiting(t, 3))
    assert t.phase == 'reconnecting: round 3 in 10 s; ' + NO_ROUTE_1.replace('round 1', 'round 2')
    clock.now = t.next_round_at - 6         # the countdown: 6 s before round 3
    t.step()
    assert t.phase.startswith('reconnecting: round 3 in 6 s; round 2: no target connects (')
    assert not any(re.search(r'round \d+/', p) for p in seen), [p for p in seen if re.search(r'round \d+/', p)]


def test_phase_names_a_timeout_reason(world):
    net, clock = world
    net.refuse = {'a': 255, 'b': 255, 'd': 255}
    net.hang = {'c'}
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 2))
    assert t.phase == ('reconnecting: round 2 in 5 s; round 1: no target connects (a: ssh exited with 255; '
                       'b: ssh exited with 255; d: ssh exited with 255; c: no healthy forward within 30 s '
                       '(no answer: [Errno 61] Connection refused))')


# ---------------------------------------------------------------------- the same failure, logged once in a while
ROUND_LINE = re.compile(r'tunnel: round (\d+)(/\d+)?: no target connects')


def round_lines(err):
    return [x for x in tunnel_lines(err) if ROUND_LINE.match(x)]


def test_the_same_failure_is_logged_once_per_interval(world, capsys):
    net, clock = world
    t = tunnel(clock, max_failed_rounds=1000)
    lose_every_route(t, clock, net)
    capsys.readouterr()
    begin = clock.now
    run(t, clock, lambda: waiting(t, 51))
    elapsed = clock.now - begin             # about 46 minutes at the 60 s cap
    err = capsys.readouterr().err
    rounds = round_lines(err)
    starts = [x for x in tunnel_lines(err) if x.startswith('tunnel: start (target 1/4)')]
    budget = elapsed / field.REPEAT_LOG_S + 3
    assert field.REPEAT_LOG_S == 600
    assert 3 <= len(rounds) <= budget, rounds
    assert len(starts) <= budget, len(starts)
    numbers = [int(ROUND_LINE.match(x).group(1)) for x in rounds]
    assert numbers[:2] == [1, 2] and all(b - a > 1 for a, b in zip(numbers[1:], numbers[2:])), numbers
    assert re.search(r'; \d+ rounds? with the same result not logged$', rounds[2]), rounds[2]


def test_a_changed_failure_is_logged_at_once(world, capsys):
    net, clock = world
    t = tunnel(clock, max_failed_rounds=1000)
    lose_every_route(t, clock, net)
    run(t, clock, lambda: waiting(t, 12))   # rounds 3-11 were the same as round 2 within 600 s: not logged
    capsys.readouterr()
    net.hang = {'c'}
    net.refuse = {'a': 255, 'b': 255, 'd': 255}
    run(t, clock, lambda: waiting(t, 13))
    rounds = round_lines(capsys.readouterr().err)
    assert len(rounds) == 1 and rounds[0].startswith('tunnel: round 12/1000: no target connects (tried in order: ')
    assert 'c: no healthy forward within 30 s' in rounds[0]
    assert rounds[0].endswith('; 9 rounds with the same result not logged'), rounds[0]
    run(t, clock, lambda: t.round == 13 and t.child is not None)
    assert 'tunnel: start (target 1/4)' in capsys.readouterr().err   # the round after a change is logged in full


def test_recovery_after_rounds_not_logged_names_them_and_the_switch(world, capsys):
    net, clock = world
    t = tunnel(clock, max_failed_rounds=1000)
    lose_every_route(t, clock, net)
    run(t, clock, lambda: waiting(t, 8))
    capsys.readouterr()
    net.refuse = {'a': 255, 'b': 255, 'd': 255}
    up_on(t, clock, 'c')
    got = tunnel_lines(capsys.readouterr().err)
    note = 'tunnel: 5 rounds with the same result not logged (rounds 3-7)'
    assert note in got and 'tunnel: switch none -> c: recovered' in got
    assert got.index(note) < got.index('tunnel: switch none -> c: recovered')
    assert any(x.startswith('tunnel: up via c') for x in got)


# ---------------------------------------------------------------------- back at once after a wake-up
def test_wake_and_network_check_parameters_default_and_project_block():
    block = read_block(ROOT / 'claude_route.yaml', 'field')
    assert DEFAULTS['wake_jump_s'] == 10 and block['wake_jump_s'] == 10
    assert DEFAULTS['net_check_interval_s'] == 1 and block['net_check_interval_s'] == 1


@pytest.mark.parametrize('key,value', [('wake_jump_s', 0.5), ('wake_jump_s', 86401), ('wake_jump_s', '10'),
                                       ('net_check_interval_s', 0.1), ('net_check_interval_s', 61),
                                       ('net_check_interval_s', True)])
def test_bad_wake_and_network_check_parameters_are_refused(key, value):
    with pytest.raises(ConfigError, match=key):
        Tunnel.from_block({'machine': HOME_MACHINE, key: value})


def live(t, clock, seconds, each=None):
    """The machine is awake for ``seconds`` of monotonic time: step as the host loop does."""
    end = clock.now + seconds
    while clock.now < end:
        delay = t.step()
        if each:
            each()
        clock.advance(min(delay, end - clock.now))


def opened_within(t, clock, net, limit):
    """Step until local_port listens; the applet's own waiting (the fake ssh connects at once) is <= limit."""
    begin = clock.now
    run(t, clock, net.listening)
    assert clock.now - begin <= limit, clock.now - begin
    return clock.now - begin


def test_eight_hours_asleep_with_short_wakes_then_the_lid_opens_with_network(world, capsys):
    """Addendum 5: lid closed for 8 hours; the system wakes up for 20 s every 15 minutes without network; then
    the lid opens with network. The port opens within 2 s of the applet's own waiting, the process never exits,
    and no short wake slows the next one down."""
    net, clock = world
    t = tunnel(clock)
    t.start()
    up_on(t, clock, 'a')
    net.freeze()                            # the lid closes: the connections die, ssh does not know yet
    net.refuse, net.sig = {d: 255 for d in TARGETS}, (None, None)
    rounds, closed = [], []
    for _ in range(32):
        clock.sleep(900 - 20)
        before = len(net.started)
        live(t, clock, 2, each=lambda: rounds.append(t.round) or closed.append(not net.listening()))
        assert len(net.started) >= before + len(TARGETS), 'no round in the first 2 s of a short wake'
        live(t, clock, 18, each=lambda: rounds.append(t.round) or closed.append(not net.listening()))
        assert t.fatal is None
    assert all(closed[1:]) and max(rounds) <= 4, max(rounds)   # 3 failed rounds per 20 s wake; next is 4
    clock.sleep(900)
    net.refuse, net.sig = {}, ('198.51.100.7', None)
    opened_within(t, clock, net, 2)
    assert t.healthy and t.fatal is None
    assert 'tunnel: woke up (clock jump ' in capsys.readouterr().err


def test_wake_up_starts_a_round_at_once_and_from_the_first_wait(world, capsys):
    """Addendum 1 and 4: waiting at the 60 s cap, asleep for 30 minutes; on wake-up a round starts at once and
    the wait starts over at restart_delay_s."""
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 8))
    clock.sleep(1800)                       # still no route after the wake-up
    begin = len(net.started)
    live(t, clock, 2)
    assert len(net.started) == begin + len(TARGETS) and t.round == 2
    assert t.phase.startswith('reconnecting: round 2 in ')
    assert t.next_round_at - t.clock() <= 5
    assert re.search(r'tunnel: woke up \(clock jump 18\d\d s\)', capsys.readouterr().err)
    clock.sleep(1800)
    net.refuse = {}
    opened_within(t, clock, net, 2)


def test_wake_jump_s_is_the_threshold(world, capsys):
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock, wake_jump_s=30)
    t.start()
    run(t, clock, lambda: waiting(t, 8))
    clock.sleep(20)
    live(t, clock, 2)
    assert 'woke up' not in capsys.readouterr().err and t.round == 8
    clock.sleep(40)
    live(t, clock, 2)
    assert 'tunnel: woke up (clock jump 40 s)' in capsys.readouterr().err and t.round == 2


def test_a_late_loop_counts_as_a_wake(world, capsys):
    """A monotonic clock that runs during sleep: the loop comes back far later than the delay it asked for."""
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 8))
    t.step()
    clock.advance(1800)
    net.refuse = {}
    t.step()
    assert re.search(r'tunnel: woke up \(clock jump 1[78]\d\d s\)', capsys.readouterr().err)
    opened_within(t, clock, net, 2)


def test_wake_up_probes_every_route_and_restarts_the_dead_ones_at_once(world, capsys):
    """Addendum 2: every route up, asleep for an hour, every connection dead. No wait for ServerAlive,
    unhealthy_restart_s (60 s) or failover_after_fails (30 s): the dead ssh children are stopped and new ones
    open the port within 2 s; the wake-up is not a drop of any route."""
    net, clock = world
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: len(t.routes) == len(TARGETS) and all(r.up for r in t.routes) and t.healthy)
    old = [c for c in net.children if c.poll() is None]
    net.freeze()
    clock.sleep(3600)
    run(t, clock, lambda: t.healthy and t.current.child not in old)
    assert all(c.poll() is not None for c in old)
    assert all(len(t.route(d).drops) == 0 for d in TARGETS)
    err = capsys.readouterr().err
    assert 'tunnel: woke up (clock jump 3600 s); probing every route now' in err
    assert 'no answer after wake-up' in err


def test_wake_up_reopens_within_2_s(world):
    net, clock = world
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: len(t.routes) == len(TARGETS) and all(r.up for r in t.routes) and t.healthy)
    old = [c for c in net.children if c.poll() is None]
    net.freeze()
    clock.sleep(3600)
    begin = clock.now
    run(t, clock, lambda: t.healthy and t.current.child not in old and net.listening())
    assert clock.now - begin <= 2, clock.now - begin


def test_network_change_starts_a_round_at_once(world, capsys):
    """Addendum 3: waiting at the 60 s cap without network; the network comes back (a default route appears):
    a round starts within net_check_interval_s, not at the end of the wait."""
    net, clock = world
    net.refuse, net.sig = {d: 255 for d in TARGETS}, (None, None)
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 8))
    clock.advance(t.step())
    net.refuse, net.sig = {}, ('203.0.113.9', None)
    opened_within(t, clock, net, 2)
    assert 'tunnel: network changed (none -> 203.0.113.9); round now' in capsys.readouterr().err


def test_an_unchanged_network_keeps_the_wait(world):
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 8))
    due = t.next_round_at
    run(t, clock, lambda: t.child is not None)
    assert clock.now >= due


def test_short_wakes_never_add_up_to_max_failed_rounds(world):
    """Addendum 4: a positive max_failed_rounds counts only the rounds of one wake-up."""
    net, clock = world
    net.refuse, net.sig = {d: 255 for d in TARGETS}, (None, None)
    t = tunnel(clock, max_failed_rounds=3)
    t.start()
    for _ in range(40):
        live(t, clock, 8)                  # two failed rounds (5 s apart)
        clock.sleep(900)
    assert t.fatal is None


def test_network_check_sends_nothing_and_never_names_the_api(monkeypatch):
    """network_signature() is a routing lookup: UDP sockets connected to documentation addresses, no data."""
    calls = []

    class Sock:
        def __init__(self, family, kind):
            calls.append(('socket', family, kind))

        def connect(self, addr):
            calls.append(('connect', addr[0]))

        def getsockname(self):
            return ('203.0.113.9', 0)

        def send(self, *_):
            raise AssertionError('sent data')

        sendto = sendall = send

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(field.socket, 'socket', Sock)
    assert field.network_signature() == ('203.0.113.9', '203.0.113.9')
    import socket as real
    assert {c[2] for c in calls if c[0] == 'socket'} == {real.SOCK_DGRAM}
    assert sorted(c[1] for c in calls if c[0] == 'connect') == ['192.0.2.1', '2001:db8::1']


# ---------------------------------------------------------------------- reviewer pre-flags
def all_up(t, clock):
    run(t, clock, lambda: len(t.routes) == len(TARGETS) and all(r.up for r in t.routes) and t.healthy)


def test_b23_an_old_ssh_of_our_own_that_will_not_exit_is_not_another_program(world, capsys):
    """B2.3: after a wake-up the dead route's old ssh ignores SIGTERM and SIGKILL for a while and keeps its route
    port. That is this applet's own child, not another program: no exit 78, no crash; the other routes open the
    port within 2 s, and the route comes back once its old ssh is gone."""
    net, clock = world
    t = tunnel(clock)
    t.start()
    all_up(t, clock)
    old = [c for c in net.children if c.poll() is None]
    stuck = t.route('a').child
    net.stubborn = {stuck.pid}
    net.freeze()
    clock.sleep(3600)
    begin = clock.now
    run(t, clock, lambda: t.healthy and t.current.child not in old and net.listening())
    assert clock.now - begin <= 2 and t.fatal is None
    assert stuck.poll() is None and t.route('a').state == 'down'
    assert 'tunnel: ssh pid %d of a does not exit; port %d stays with it' % (stuck.pid, stuck.port) in \
        capsys.readouterr().err
    run(t, clock, lambda: clock.now - begin > 130)   # two measure cycles: the port is still the old ssh's
    assert t.fatal is None and t.route('a').state == 'down'
    stuck.exit(-9)                          # it finally ends
    net.stubborn = set()
    run(t, clock, lambda: t.route('a').up)
    assert t.fatal is None


def test_b24_every_step_pauses_at_most_net_check_interval_s(world):
    """B2.4/B1.3: with a route in use the loop still comes back within net_check_interval_s, so a wake-up is
    seen within that long (the host's wait does not run during sleep)."""
    net, clock = world
    for interval in (1, 0.5):
        t = tunnel(clock, net_check_interval_s=interval)
        t.start()
        delays = []
        for _ in range(3000):
            delay = t.step()
            delays.append((t.current is not None, delay))
            clock.advance(delay)
        assert any(up for up, _ in delays)
        assert max(d for _, d in delays) <= interval, max(d for _, d in delays)
        t.close()


def test_b25_wake_up_probes_use_the_short_timeout(world):
    """B2.5: the wake-up probe of each route waits wake_probe_timeout_s (default 1), not probe_timeout_s (5)."""
    net, clock = world
    t = tunnel(clock)
    t.start()
    all_up(t, clock)
    net.freeze()
    clock.sleep(3600)
    net.timeouts = []
    clock.advance(t.step())                 # the step that sees the wake-up probes every route
    assert len(net.timeouts) >= len(TARGETS) and set(net.timeouts) == {1.0}, net.timeouts
    assert DEFAULTS['wake_probe_timeout_s'] == 1
    assert read_block(ROOT / 'claude_route.yaml', 'field')['wake_probe_timeout_s'] == 1
    t2 = tunnel(clock, wake_probe_timeout_s=0.5)
    assert t2.wake_probe_timeout_s == 0.5


@pytest.mark.parametrize('value', [0, 61, '1', True])
def test_b25_bad_wake_probe_timeout_s_is_refused(value):
    with pytest.raises(ConfigError, match='wake_probe_timeout_s'):
        Tunnel.from_block({'machine': HOME_MACHINE, 'wake_probe_timeout_s': value})


def test_b33_an_available_network_that_changes_does_not_start_a_round_each_check(world):
    """B3.3: only "no network -> network" starts a round at once. With a network that keeps changing (two
    addresses in turn) while home is down, rounds follow the wait, not every network check."""
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    addresses = [('198.51.100.7', None), ('198.51.100.8', None)]
    end = clock.now + 600
    n = 0
    while clock.now < end:
        net.sig = addresses[n % 2]
        n += 1
        clock.advance(t.step())
    assert net.started.count('a') <= 15, net.started.count('a')   # the waits 5, 10, 20, 40, 60 ... in 600 s


def test_b35_network_check_resolves_no_name(monkeypatch):
    """B3.5: no DNS (an ssh alias is not a host name) and no connection: a routing lookup of numeric
    documentation addresses."""
    import ipaddress

    def refuse(*a, **k):
        raise AssertionError('name lookup or connection: %r' % (a,))

    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'create_connection'):
        monkeypatch.setattr(field.socket, name, refuse)
    out = field.network_signature()
    assert isinstance(out, tuple) and len(out) == 2
    docs = [ipaddress.ip_network('192.0.2.0/24'), ipaddress.ip_network('2001:db8::/32')]
    assert all(any(ipaddress.ip_address(a) in n for n in docs) for _, a in field.ROUTE_LOOKUP)


def test_a25_a_network_check_that_sees_no_network_does_not_hold_back_the_round(world):
    """A2.5: the network check only brings a round forward. If it says "no network" while home is reachable,
    the round still starts when its wait ends."""
    net, clock = world
    net.refuse, net.sig = {d: 255 for d in TARGETS}, (None, None)
    t = tunnel(clock)
    t.start()
    run(t, clock, lambda: waiting(t, 8))
    due = t.next_round_at
    net.refuse = {}                         # home is back; the check still sees no default route
    run(t, clock, net.listening)
    assert due <= clock.now <= due + 1


def test_a44_eight_hours_awake_without_network_log_every_kind_of_line_rarely(world, capsys):
    """A4.4: the rate limit covers every line of a round (start, failed, measure, round), not only the round
    line: 8 hours of failed rounds give a few hundred lines, not tens of thousands."""
    net, clock = world
    net.refuse = {d: 255 for d in TARGETS}
    t = tunnel(clock)
    t.start()
    capsys.readouterr()
    end = clock.now + 8 * 3600
    for _ in range(100000):
        if clock.now >= end:
            break
        clock.advance(t.step())
    lines = tunnel_lines(capsys.readouterr().err)
    groups = 8 * 3600 / field.REPEAT_LOG_S + 3
    assert t.round > 400
    for kind in ('tunnel: start (target', 'tunnel: a failed:', 'tunnel: measure a ', 'tunnel: round '):
        got = [x for x in lines if x.startswith(kind)]
        assert len(got) <= groups * (len(TARGETS) if kind.startswith('tunnel: start') else 1), (kind, len(got))
    assert len(lines) <= groups * (3 * len(TARGETS) + 1), len(lines)


def test_b43_short_wakes_never_count_toward_a_positive_max_failed_rounds(world):
    """B4.3 (ruling): failed rounds of a short wake count for nothing, a positive max_failed_rounds included;
    30 s wakes fail 3 rounds each with max_failed_rounds 3 and the applet never exits."""
    net, clock = world
    net.refuse, net.sig = {d: 255 for d in TARGETS}, (None, None)
    t = tunnel(clock, max_failed_rounds=3)
    t.start()
    live(t, clock, 8)                       # two failed rounds before the lid closes
    for _ in range(30):
        clock.sleep(900)
        live(t, clock, 30)
        assert t.fatal is None
    net.refuse, net.sig = {}, ('198.51.100.7', None)
    clock.sleep(900)
    opened_within(t, clock, net, 2)


def test_b43_a_long_wake_without_network_still_ends_at_max_failed_rounds(world, capsys):
    """The other side of the ruling: awake for longer than a short wake, without network, a positive
    max_failed_rounds still ends the applet with 78, counting only the rounds after field.SHORT_WAKE_S."""
    net, clock = world
    net.refuse, net.sig = {d: 255 for d in TARGETS}, (None, None)
    t = tunnel(clock, max_failed_rounds=3)
    t.start()
    live(t, clock, 8)
    clock.sleep(900)
    woke = clock.now
    with pytest.raises(SystemExit) as exc:
        run(t, clock, lambda: False)
    assert exc.value.code == EXIT_CONFIG and clock.now - woke >= field.SHORT_WAKE_S == 60
    assert 'connects in 3 rounds in a row' in capsys.readouterr().err
