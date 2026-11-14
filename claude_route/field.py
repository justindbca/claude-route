"""applet-tunnel-1: ssh routes from the field machine to the home proxy; new connections take the best one.

The ssh targets come only from the fleet-superpowers inventory: ``machines.<machine>.reach[].ssh``, in the
order they are listed there. The file is ``$FLEETMUX_SUPERPOWERS`` when set, else
``~/.fleetmux/fleet-superpowers/fleet-superpowers.yaml`` (the same rule as fleetmux). The applet only reads it.

Every target (a route) gets its own ssh child,
``ssh -N -L 127.0.0.1:<route_port>:127.0.0.1:<remote_port> <target>``, with BatchMode, ExitOnForwardFailure,
ServerAlive and no connection sharing (ControlMaster=no, ControlPath=none). Route ports come from
``route_ports`` (a range that avoids the reserved ports and ``local_port``); a route keeps its port while it
stays in the inventory; targets past the size of the range are skipped (logged once). Sessions connect only to ``127.0.0.1:<local_port>``: a loopback relay
(``claude_route.relay``) there hands each new connection to the route in use; a switch moves new connections
only, existing ones stay on their route until they end or that route's ssh goes away.

- Measuring. A probe is ``GET /__claude_route/health`` through a route's own port, within
  ``probe_timeout_s``, answered with the home proxy's service name. At start every route is started at once
  (in list order); a route is up when a probe passes within ``attempt_timeout_s``, then it gets ``samples``
  probes ``sample_gap_s`` apart. The route in use is probed every ``check_interval_s``; every other route gets
  ``samples`` probes every ``measure_interval_s``, when the inventory is also read again and routes that are
  down are started again.
- Score (lower is better): the median of the last ``samples`` good probe times in the last ``window_s``, plus
  ``fail_penalty_ms`` times the share of failed probes in the window, plus ``drop_penalty_ms`` per drop in the
  window. A route with no good probe in the window has no score. Equal scores (whole ms): list order.
- The first route that comes up is used at once. When every route of the start (or of the round that ends a
  time without any route) has been measured, the best one is taken if it beats the one in use by the margin. After that a better route is taken only when its
  score is below ``(1 - switch_margin_ratio)`` times the one in use and at least ``switch_margin_ms`` lower,
  for ``switch_hold_s`` without a break, and not within ``switch_cooldown_s`` of the last switch between
  routes. A drop of the route in use, or ``failover_after_fails`` failed probes in a row on it, moves to the
  best other route at once. A target for a switch must have passed its last probe.
- A drop is an ssh exit after the route was up, or failing probes for ``unhealthy_restart_s``. A few failed
  probes are not a drop: they only count in the score and towards ``failover_after_fails``.
- No route: when the route in use drops and no other route has a score, ``127.0.0.1:<local_port>`` is closed
  (sessions wait at launch). ``restart_delay_s`` later a round starts every route again; a round in which none
  comes up is followed by another one after a wait that starts at ``restart_delay_s`` and doubles after each
  failed round in a row, up to ``restart_delay_max_s``; a route that comes up starts the wait over. A network
  that is gone is never a reason to stop: by default (``max_failed_rounds`` 0) the rounds go on until a route
  comes up. A positive ``max_failed_rounds`` keeps the old exit: that many failed rounds in a row end the
  applet with exit code 78 and one stderr line naming every target of the last round.
- Wake-up: when the clocks jump by ``wake_jump_s`` or more between two loops (the wall clock against the
  monotonic one, which stands still during sleep; or a loop later than its own delay), the applet probes every
  route that is up at once and stops and restarts each one that does not answer (not a drop), and with no
  route starts a round at once with the wait and the round count starting over. The wake-up probe waits
  ``wake_probe_timeout_s``. Rounds that fail within ``SHORT_WAKE_S`` of a wake-up (a short system wake in the
  night) never count toward ``max_failed_rounds``. The main loop never pauses longer than
  ``net_check_interval_s``. An old ssh child of this applet that does not end keeps its route port; that route
  waits for it (it is not another program).
- Network change: with no route, the default routes are looked up every ``net_check_interval_s``
  (``network_signature``: UDP sockets connected to numeric documentation addresses, no name lookup, nothing is
  sent); when there was no network and now there is, a round starts at once and the wait starts over. The check
  never holds a round back: with no network seen, a round still starts when its wait ends.
- Rounds that fail for the same reasons as the round before are logged once per ``REPEAT_LOG_S``: the round
  lines in between (start, failed, measure, round) are left out, and the next line logged counts them.
- SIGTERM: every ssh child is stopped (SIGTERM, then SIGKILL after ``stop_wait_s``), the relay is shut down,
  the applet waits until ``local_port`` and the route ports refuse connections, then exits 0.

Log lines (stderr): ``tunnel: measure <target> port <p> ok <k>/<n> median <ms> fail <f>/<t> drops <d>
cost <ms|inf> <current|standby>[ last: <reason>]`` after each measurement, and ``tunnel: switch <from> ->
<to>: <reason>`` for each switch. There is no other route: never a direct connection.
"""
import math
import os
import queue
import re
import signal
import socket
import statistics
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from claude_route import (EXIT_CONFIG, LOOPBACK, ConfigError, check_port, check_seconds, log, main, settings)
from claude_route.home import health_ok

INVENTORY_ENV = 'FLEETMUX_SUPERPOWERS'
INVENTORY_DEFAULT = Path('~/.fleetmux/fleet-superpowers/fleet-superpowers.yaml')
# Loopback ports other programs on the field machine own or reserve; local_port and route_ports avoid them.
RESERVED_PORTS = (7890, 9090, 9091, 17890, 19091, 18888, 16379, 16380, 18790, 18793, 14001)
DEFAULTS = {
    'machine': None,               # required, no default: the home machine's name in the machines block of
                                   # fleet-superpowers
    'local_port': 17892,           # loopback port on this machine (the session_env port), served by the relay
    'remote_port': 17891,          # the home proxy's port on the home machine's loopback
    'route_ports': [17900, 17915], # loopback ports of the routes' ssh forwards, first and last
    'ssh': ['ssh'],                # the ssh command (tests use a fake)
    'ssh_options': [],             # extra "-o Key=Value" options, after the fixed ones (first value wins)
    'server_alive_interval_s': 15,
    'server_alive_count_max': 3,
    'connect_timeout_s': 10,
    'attempt_timeout_s': 30,       # one route: ssh start to the first good probe
    'restart_delay_s': 5,          # no route left: wait before a round, and the first wait between rounds
    'restart_delay_max_s': 60,     # the wait between rounds doubles after each failed round, up to this
    'max_failed_rounds': 0,        # rounds in a row in which no route comes up before exit 78; 0: no limit
    'wake_jump_s': 10,             # a clock jump this long between two loops is a wake-up: check routes now
    'net_check_interval_s': 1,     # no route: network check period; also the longest pause of the main loop
    'wake_probe_timeout_s': 1,     # the probe of each route right after a wake-up; slower: restarted at once
    'check_interval_s': 10,        # probe period of the route in use
    'unhealthy_restart_s': 60,     # up, then probes failing this long: a drop
    'stop_wait_s': 5,              # child stop and listener-gone confirmation, each
    'probe_timeout_s': 5,          # one probe; slower is a failed probe
    'samples': 5,                  # probes per measurement, and good probe times the median is taken over
    'sample_gap_s': 1,             # between the probes of one measurement
    'measure_interval_s': 60,      # measurement period of the other routes; inventory read again
    'window_s': 600,               # the score looks at this much history
    'fail_penalty_ms': 5000,       # score added at a failed-probe share of 1
    'drop_penalty_ms': 2000,       # score added per drop in the window
    'switch_margin_ratio': 0.3,    # a better route must score at most (1 - this) times the one in use
    'switch_margin_ms': 50,        # ... and at least this much lower
    'switch_hold_s': 120,          # ... for this long without a break
    'switch_cooldown_s': 300,      # no switch for a better route this long after a switch between routes
    'failover_after_fails': 3,     # failed probes in a row on the route in use before a forced switch
}
NAME_RE = re.compile(r'[A-Za-z0-9_.-]+\Z')
MACHINE_MISSING = ('machine is not set: set field.machine in claude_route.yaml to the home machine\'s name in '
                   'the machines block of the fleet-superpowers inventory (fleetmux superpowers show --machines)')
OPTION_RE = re.compile(r'[A-Za-z]+=[^\s]+\Z')
CONNECT_POLL_S = 0.5               # probe period of a route that is still connecting
PROBE_WAIT_S = 0.2                 # step period while a probe is in flight
REPEAT_LOG_S = 600                 # rounds failing for the same reasons are logged once per this many seconds
SHORT_WAKE_S = 60                  # failed rounds this soon after a wake-up count toward no max_failed_rounds


def port_open(port, host=LOOPBACK, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Documentation addresses (RFC 5737, RFC 3849): a UDP connect() to them is a routing lookup only, nothing is sent.
ROUTE_LOOKUP = ((socket.AF_INET, '192.0.2.1'), (socket.AF_INET6, '2001:db8::1'))


def network_signature():
    """(IPv4 source address, IPv6 source address) the kernel picks for its default routes; None for a family
    without one. No packet leaves the machine and no real host is named."""
    out = []
    for family, addr in ROUTE_LOOKUP:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((addr, 9))
                out.append(s.getsockname()[0])
        except OSError:
            out.append(None)
    return tuple(out)


def health_probe(port, timeout):
    """(ok, info, elapsed_ms) of one health request through ``127.0.0.1:<port>``; info names the home proxy's
    pid when ok, else the reason."""
    begin = time.monotonic()
    ok, info = health_ok(port, timeout=timeout)
    elapsed_ms = (time.monotonic() - begin) * 1000.0
    return ok, ('home proxy pid %s' % info.get('pid') if ok else info), elapsed_ms


def run_async(fn):
    """The production probe executor: one thread per probe, off the main loop."""
    threading.Thread(target=fn, name='claude-route-probe', daemon=True).start()


def make_relay(port, choose, on_route_refused):
    from claude_route.relay import LoopbackRelay
    return LoopbackRelay(port, choose, on_route_refused=on_route_refused)


def inventory_path():
    env = os.environ.get(INVENTORY_ENV)
    return Path(env).expanduser() if env else INVENTORY_DEFAULT.expanduser()


def reach(machine, path=None):
    """The ssh destinations of ``machines.<machine>.reach`` in the inventory, in their listed order."""
    import yaml
    path = Path(path) if path else inventory_path()
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError('cannot read the fleet-superpowers inventory %s: %s'
                          % (path, str(exc).splitlines()[0][:200])) from None
    machines = data.get('machines') if isinstance(data, dict) else None
    if not isinstance(machines, dict) or machine not in machines:
        names = ', '.join(sorted(machines)) if isinstance(machines, dict) and machines else 'none'
        raise ConfigError('no machine %s in the machines block of %s (machines: %s)' % (machine, path, names))
    entry = machines[machine]
    items = entry.get('reach') if isinstance(entry, dict) else None
    if not isinstance(items, list) or not items:
        raise ConfigError('machines.%s.reach in %s lists no ssh destination' % (machine, path))
    out = []
    for i, item in enumerate(items):
        dest = item.get('ssh') if isinstance(item, dict) else None
        if not (isinstance(dest, str) and dest and not dest.startswith('-') and dest.split() == [dest]):
            raise ConfigError('machines.%s.reach[%d].ssh in %s must be a Host alias or user@host, got %r'
                              % (machine, i, path, dest))
        out.append(dest)
    return out


def check_number(name, value, low, high, integer=False):
    kinds = (int,) if integer else (int, float)
    if type(value) not in kinds or not low <= value <= high:
        raise ConfigError('%s must be %s %s-%s, got %r' % (name, 'an integer' if integer else 'a number', low, high,
                                                           value))
    return value


def check_route_ports(value, local_port):
    if not (isinstance(value, list) and len(value) == 2 and all(type(p) is int for p in value)):
        raise ConfigError('route_ports must be [first, last], two loopback ports, got %r' % (value,))
    low, high = value
    if not (1024 <= low <= high <= 65535):
        raise ConfigError('route_ports must be [first, last] with 1024 <= first <= last <= 65535, got %r' % (value,))
    clash = [p for p in RESERVED_PORTS if low <= p <= high]
    if clash:
        raise ConfigError('route_ports %d-%d holds the reserved port(s) %s; avoid %s'
                          % (low, high, ', '.join(map(str, clash)), ', '.join(map(str, RESERVED_PORTS))))
    if low <= local_port <= high:
        raise ConfigError('route_ports %d-%d holds local_port %d' % (low, high, local_port))
    return low, high


def ports_for(route_ports, targets, machine):
    low, high = route_ports
    if len(targets) > high - low + 1:
        raise ConfigError('route_ports %d-%d has %d port(s) for the %d routes of machine %s; widen route_ports'
                          % (low, high, high - low + 1, len(targets), machine))


def ms(value):
    return 'inf' if math.isinf(value) else '%dms' % round(value)


class Route:
    """One ssh target, its forward port and its measurements."""

    def __init__(self, dest, port):
        self.dest, self.port = dest, port
        self.child = None
        self.state = 'down'        # down | connecting | up
        self.gen = 0               # bumped at each start and stop; a probe result of an older one is dropped
        self.down_why = ''
        self.attempt_deadline = 0.0
        self.attempt_probes = 0
        self.last_info = ''
        self.probing = False
        self.next_probe = None
        self.burst_left = self.burst_ok = self.burst_n = 0
        self.burst_fail = None
        self.results = deque()     # (time, ok, seconds) of the probes since the route came up
        self.drops = deque()       # times of drops
        self.fails_in_row = 0
        self.fail_since = None
        self.last_ok = False
        self.wake_check = False    # up before a wake-up; not used until its next probe answers

    @property
    def up(self):
        return self.state == 'up'


class Tunnel:
    def __init__(self, machine, targets, local_port=17892, remote_port=17891, route_ports=(17900, 17915),
                 ssh=('ssh',), ssh_options=(), server_alive_interval_s=15, server_alive_count_max=3,
                 connect_timeout_s=10, attempt_timeout_s=30.0, restart_delay_s=5.0, restart_delay_max_s=60.0,
                 check_interval_s=10.0, unhealthy_restart_s=60.0, stop_wait_s=5.0, max_failed_rounds=0,
                 wake_jump_s=10.0, net_check_interval_s=1.0, wake_probe_timeout_s=1.0, probe_timeout_s=5.0,
                 samples=5,
                 sample_gap_s=1.0, measure_interval_s=60.0, window_s=600.0, fail_penalty_ms=5000,
                 drop_penalty_ms=2000, switch_margin_ratio=0.3, switch_margin_ms=50, switch_hold_s=120.0,
                 switch_cooldown_s=300.0, failover_after_fails=3, clock=time.monotonic, wall=time.time,
                 probe=None, executor=None, net_probe=None):
        self.machine = machine
        self.targets = list(targets)
        self.local_port = local_port
        self.remote_port = remote_port
        self.route_ports = tuple(route_ports)
        self.ssh = list(ssh)
        self.ssh_options = list(ssh_options)
        self.server_alive_interval_s = int(server_alive_interval_s)
        self.server_alive_count_max = int(server_alive_count_max)
        self.connect_timeout_s = int(connect_timeout_s)
        self.attempt_timeout_s = attempt_timeout_s
        self.restart_delay_s = restart_delay_s
        self.restart_delay_max_s = restart_delay_max_s
        self.check_interval_s = check_interval_s
        self.unhealthy_restart_s = unhealthy_restart_s
        self.stop_wait_s = stop_wait_s
        self.max_failed_rounds = int(max_failed_rounds)
        self.wake_jump_s = wake_jump_s
        self.net_check_interval_s = net_check_interval_s
        self.wake_probe_timeout_s = wake_probe_timeout_s
        self.probe_timeout_s = probe_timeout_s
        self.samples = int(samples)
        self.sample_gap_s = sample_gap_s
        self.measure_interval_s = measure_interval_s
        self.window_s = window_s
        self.fail_penalty_ms = fail_penalty_ms
        self.drop_penalty_ms = drop_penalty_ms
        self.switch_margin_ratio = switch_margin_ratio
        self.switch_margin_ms = switch_margin_ms
        self.switch_hold_s = switch_hold_s
        self.switch_cooldown_s = switch_cooldown_s
        self.failover_after_fails = int(failover_after_fails)
        self.clock = clock
        self.wall = wall
        # probe(route_port[, timeout]) -> (ok, info, elapsed_ms); executor(fn) runs one probe (a thread; tests:
        # inline). The timeout is passed only for the wake-up probe (wake_probe_timeout_s).
        self.probe = probe or (lambda port, timeout=None: health_probe(
            port, self.probe_timeout_s if timeout is None else timeout))
        self.executor = executor or run_async
        self.net_probe = net_probe or (lambda: network_signature())
        self.last_tick = None      # (wall, monotonic, delay asked for) of the last step: wake-up detection
        self.retry_now = False     # a route failed its wake-up probe: the round after a loss starts at once
        self.net_sig = None        # network_signature() at the last round start or check
        self.next_net_check = 0.0
        self.woke_at = None        # monotonic time of the last wake-up
        self.counted = 0           # failed rounds in a row that count toward max_failed_rounds
        self.lingering = []        # (child, route port): own ssh children that did not end when stopped
        self.routes = []
        self.skipped = set()       # inventory targets past the size of route_ports (logged once)
        self.current = None        # the Route new connections go to; None: local_port is closed
        self.settling = True       # a margin-only pick is due once every route of the round is measured
        self.settle_kind = 'startup'
        self.ever_up = False
        self.round = 1             # current round; > 1 after rounds in a row in which no route came up
        self.reconnecting = False  # the route in use was lost, or a round failed, since a route was last in use
        self.in_round = False      # a round runs (no route in use)
        self.next_round_at = None
        self.wait_note = ''
        self.failures = []         # "target: reason" of the current round
        self.last_failures = []    # ... of the last failed round (shown in the phase)
        self.last_sig = None       # the sorted reasons of the last failed round
        self.repeat = False        # the last failed round failed for the same reasons as the one before
        self.quiet = False         # the current round's lines are not logged (a repeat within REPEAT_LOG_S)
        self.logged_at = None      # when a failed round was last logged in full
        self.unlogged = []         # numbers of the failed rounds not logged since then
        self.next_measure = 0.0
        self.switched_at = None
        self.last_switch = 'never'
        self.cooldown_until = float('-inf')
        self.better = None         # (route, since): a route that beats the one in use by the margin
        self.choice = ()           # route ports for a new connection, in order (read by relay threads)
        self.results = queue.Queue()
        self.refused = queue.Queue()  # route ports the relay found refusing (relay threads put, step drains)
        self.relay = None
        self.local_port_taken = False  # start() found another program on local_port
        self.starts = 0
        self.fatal = None
        self.phase = 'starting'

    # ------------------------------------------------------------------ config
    @classmethod
    def from_block(cls, block):
        s = settings(block, DEFAULTS)
        machine = s['machine']
        if machine is None:
            raise ConfigError(MACHINE_MISSING)
        if not (isinstance(machine, str) and NAME_RE.match(machine)):
            raise ConfigError('machine must be a machine name of the fleet-superpowers inventory, got %r'
                              % (machine,))
        local_port = check_port('local_port', s['local_port'])
        if local_port in RESERVED_PORTS:
            raise ConfigError('local_port %d is reserved on the field machine; avoid %s'
                              % (local_port, ', '.join(map(str, RESERVED_PORTS))))
        route_ports = check_route_ports(s['route_ports'], local_port)
        ssh = s['ssh']
        if not (isinstance(ssh, list) and ssh and all(isinstance(a, str) and a for a in ssh)):
            raise ConfigError('ssh must be a non-empty list of strings, got %r' % (ssh,))
        opts = s['ssh_options']
        if not (isinstance(opts, list) and all(isinstance(o, str) and OPTION_RE.match(o) for o in opts)):
            raise ConfigError('ssh_options must be a list of "Key=Value" strings, got %r' % (opts,))
        for name in ('server_alive_interval_s', 'server_alive_count_max', 'connect_timeout_s'):
            if type(s[name]) is not int or not 1 <= s[name] <= 3600:
                raise ConfigError('%s must be an integer 1-3600, got %r' % (name, s[name]))
        if type(s['max_failed_rounds']) is not int or not 0 <= s['max_failed_rounds'] <= 10000:
            raise ConfigError('max_failed_rounds must be an integer 0-10000 (0: no limit), got %r'
                              % (s['max_failed_rounds'],))
        wake_jump_s = check_seconds('wake_jump_s', s['wake_jump_s'], 1, 86400)
        net_check_interval_s = check_seconds('net_check_interval_s', s['net_check_interval_s'], 0.2, 60)
        wake_probe_timeout_s = check_seconds('wake_probe_timeout_s', s['wake_probe_timeout_s'], 0.1, 60)
        restart_delay_s = check_seconds('restart_delay_s', s['restart_delay_s'], 0.1, 3600)
        restart_delay_max_s = check_seconds('restart_delay_max_s', s['restart_delay_max_s'], 0.1, 3600)
        if restart_delay_max_s < restart_delay_s:
            raise ConfigError('restart_delay_max_s must be at least restart_delay_s (%g), got %r'
                              % (restart_delay_s, s['restart_delay_max_s']))
        targets = reach(machine)
        ports_for(route_ports, targets, machine)
        if type(s['switch_margin_ratio']) not in (int, float) or not 0 <= s['switch_margin_ratio'] < 1:
            raise ConfigError('switch_margin_ratio must be a number 0 or more and below 1, got %r'
                              % (s['switch_margin_ratio'],))
        return cls(machine, targets, local_port=local_port, route_ports=route_ports,
                   remote_port=check_port('remote_port', s['remote_port']), ssh=ssh, ssh_options=opts,
                   server_alive_interval_s=s['server_alive_interval_s'],
                   server_alive_count_max=s['server_alive_count_max'],
                   connect_timeout_s=s['connect_timeout_s'],
                   attempt_timeout_s=check_seconds('attempt_timeout_s', s['attempt_timeout_s'], 0.5, 600),
                   restart_delay_s=restart_delay_s, restart_delay_max_s=restart_delay_max_s,
                   check_interval_s=check_seconds('check_interval_s', s['check_interval_s'], 0.1, 3600),
                   unhealthy_restart_s=check_seconds('unhealthy_restart_s', s['unhealthy_restart_s'], 1, 86400),
                   stop_wait_s=check_seconds('stop_wait_s', s['stop_wait_s'], 0.1, 60),
                   max_failed_rounds=s['max_failed_rounds'], wake_jump_s=wake_jump_s,
                   net_check_interval_s=net_check_interval_s, wake_probe_timeout_s=wake_probe_timeout_s,
                   probe_timeout_s=check_seconds('probe_timeout_s', s['probe_timeout_s'], 0.1, 600),
                   samples=check_number('samples', s['samples'], 1, 100, integer=True),
                   sample_gap_s=check_seconds('sample_gap_s', s['sample_gap_s'], 0, 3600),
                   measure_interval_s=check_seconds('measure_interval_s', s['measure_interval_s'], 1, 86400),
                   window_s=check_seconds('window_s', s['window_s'], 1, 7 * 86400),
                   fail_penalty_ms=check_number('fail_penalty_ms', s['fail_penalty_ms'], 0, 10 ** 7),
                   drop_penalty_ms=check_number('drop_penalty_ms', s['drop_penalty_ms'], 0, 10 ** 7),
                   switch_margin_ratio=s['switch_margin_ratio'],
                   switch_margin_ms=check_number('switch_margin_ms', s['switch_margin_ms'], 0, 10 ** 7),
                   switch_hold_s=check_seconds('switch_hold_s', s['switch_hold_s'], 0, 86400),
                   switch_cooldown_s=check_seconds('switch_cooldown_s', s['switch_cooldown_s'], 0, 86400),
                   failover_after_fails=check_number('failover_after_fails', s['failover_after_fails'], 1, 1000,
                                                     integer=True))

    def argv(self, dest, port):
        fixed = ['BatchMode=yes', 'ExitOnForwardFailure=yes',
                 'ServerAliveInterval=%d' % self.server_alive_interval_s,
                 'ServerAliveCountMax=%d' % self.server_alive_count_max,
                 'ConnectTimeout=%d' % self.connect_timeout_s,
                 'ControlMaster=no', 'ControlPath=none']
        out = self.ssh + ['-N', '-T']
        for opt in fixed + self.ssh_options:
            out += ['-o', opt]
        return out + ['-L', '%s:%d:%s:%d' % (LOOPBACK, port, LOOPBACK, self.remote_port), dest]

    # ------------------------------------------------------------------ views
    def route(self, dest):
        for r in self.routes:
            if r.dest == dest:
                return r
        raise KeyError(dest)

    @property
    def dest(self):
        return self.current.dest if self.current is not None else None

    @property
    def child(self):
        """The ssh child of the route in use, else the first one running (None: no ssh child runs)."""
        if self.current is not None:
            return self.current.child
        return next((r.child for r in self.routes if r.child is not None), None)

    @property
    def startup(self):
        """The margin-only pick of the start is still due."""
        return self.settling and self.settle_kind == 'startup'

    @property
    def healthy(self):
        return self.current is not None and self.current.last_ok

    def _choose(self):
        return list(self.choice)

    # ------------------------------------------------------------------ score
    def _prune(self, r, now):
        edge = now - self.window_s
        while r.results and r.results[0][0] < edge:
            r.results.popleft()
        while r.drops and r.drops[0] < edge:
            r.drops.popleft()

    def _median_ms(self, r):
        good = [sec for _, ok, sec in r.results if ok]
        return statistics.median(good[-self.samples:]) * 1000.0 if good else None

    def _cost(self, r, now):
        self._prune(r, now)
        median = self._median_ms(r)
        if not r.up or median is None:
            return math.inf
        failed = sum(1 for _, ok, _ in r.results if not ok)
        return (median + self.fail_penalty_ms * failed / len(r.results) + self.drop_penalty_ms * len(r.drops))

    def _rank(self, now, exclude=None, need_last_ok=True):
        """Routes with a score (and a good last probe), best first; equal whole-ms scores in list order."""
        out = []
        for i, r in enumerate(self.routes):
            if r is exclude or not r.up or r.wake_check or (need_last_ok and not r.last_ok):
                continue
            cost = self._cost(r, now)
            if not math.isinf(cost):
                out.append((round(cost), i, r))
        return [r for _, _, r in sorted(out, key=lambda x: x[:2])]

    def _beats(self, now, better, worse):
        cb, cw = self._cost(better, now), self._cost(worse, now)
        if math.isinf(cw):
            return True
        cb, cw = round(cb), round(cw)
        return cb <= cw * (1 - self.switch_margin_ratio) and cw - cb >= self.switch_margin_ms

    # ------------------------------------------------------------------ routes
    def _sync_routes(self, now):
        """Read the inventory again: new targets get a free route port, gone ones are stopped. Targets past the
        size of route_ports are skipped, in list order (the first ones are used)."""
        try:
            targets = reach(self.machine)
        except ConfigError as exc:
            self._die(str(exc))
        size = self.route_ports[1] - self.route_ports[0] + 1
        for dest in targets[size:]:
            if dest not in self.skipped:
                log('tunnel: route %s skipped: no free port in route_ports' % dest)
        self.skipped = set(targets[size:])
        targets = targets[:size]
        old = {r.dest: r for r in self.routes}
        for r in self.routes:
            if r.dest not in targets:
                self._stop_route(r)
                log('tunnel: %s is no longer in the inventory; stopped' % r.dest)
        used = {old[d].port for d in targets if d in old}
        free = iter(p for p in range(self.route_ports[0], self.route_ports[1] + 1) if p not in used)
        self.routes = [old[d] if d in old else Route(d, next(free)) for d in targets]
        self.targets = targets
        if self.current is not None and self.current.dest not in targets:
            self.current.state, self.current.down_why = 'down', 'no longer in the inventory'

    def _label(self, r):
        return '%s (target %d/%d)' % (r.dest, self.routes.index(r) + 1, len(self.routes))

    def _spawn(self, r, now):
        self.lingering = [(c, p) for c, p in self.lingering if c.poll() is None]
        if port_open(r.port):
            own = next((c for c, p in self.lingering if p == r.port), None)
            if own is not None:  # this applet's old ssh still holds the port: wait for it, not exit 78
                self._attempt_failed(r, now, 'old ssh pid %d still holds port %d' % (own.pid, r.port))
                return
            self._die('%s:%d (route port of %s) is already in use by another program; set field.route_ports to '
                      'a free range' % (LOOPBACK, r.port, r.dest))
        argv = self.argv(r.dest, r.port)
        self._say('tunnel: start (target %d/%d) %s' % (self.routes.index(r) + 1, len(self.routes), ' '.join(argv)))
        r.gen += 1
        r.probing, r.burst_left, r.attempt_probes, r.last_info = False, 0, 0, 'no answer yet'
        try:  # same process group as the applet: the host's group sweep also reaches it
            r.child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            r.child = None
            self._attempt_failed(r, now, 'cannot start ssh: %s' % exc)
            return
        self.starts += 1
        r.state = 'connecting'
        r.attempt_deadline = now + self.attempt_timeout_s
        r.next_probe = now

    def _stop_child(self, r):
        child, r.child = r.child, None
        if child is None or child.poll() is not None:
            return
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(self.stop_wait_s)
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(self.stop_wait_s)
            except subprocess.TimeoutExpired:
                self.lingering.append((child, r.port))
                log('tunnel: ssh pid %d of %s does not exit; port %d stays with it' % (child.pid, r.dest, r.port))
                return
        self._say('tunnel: ssh pid %d stopped (%s)' % (child.pid, child.returncode))

    def _stop_route(self, r):
        self._stop_child(r)
        self._gone(r)

    def _gone(self, r):
        r.state, r.child, r.probing, r.next_probe, r.burst_left = 'down', None, False, None, 0
        r.gen += 1
        r.wake_check = False

    def _attempt_failed(self, r, now, why):
        self._say('tunnel: %s failed: %s' % (r.dest, why))
        self._gone(r)
        self._log_measure(r, now, attempt=True, last=why)
        if self.current is None and self.in_round:
            self.failures.append('%s: %s' % (r.dest, why))

    def _dropped(self, r, now, why, count=True):
        log('tunnel: %s dropped: %s' % (r.dest, why))
        self._gone(r)
        if count:
            r.drops.append(now)
        r.down_why = why

    # ------------------------------------------------------------------ probes
    def _launch_probes(self, now):
        for r in self.routes:
            if r.state == 'down' or r.probing or r.next_probe is None or r.next_probe > now:
                continue
            r.probing, r.next_probe = True, None
            self._probe_off_loop(r, r.gen, r.port)

    def _probe_off_loop(self, r, gen, port):
        results, probe = self.results, self.probe
        timeout = self.wake_probe_timeout_s if r.wake_check else None

        def work():
            ok, info, elapsed_ms = probe(port) if timeout is None else probe(port, timeout)
            results.put((r, gen, ok, info, elapsed_ms / 1000.0))

        self.executor(work)

    def _on_route_refused(self, port):
        """Relay thread: a route port refused a relayed connection."""
        self.refused.put(port)

    def _drain_refused(self, now):
        while True:
            try:
                port = self.refused.get_nowait()
            except queue.Empty:
                return
            r = next((x for x in self.routes if x.port == port and x.up), None)
            if r is None:
                continue
            why = 'relayed connection refused on port %d' % port
            log('tunnel: %s failed: %s' % (r.dest, why))
            r.results.append((now, False, 0.0))
            self._prune(r, now)
            r.last_ok = False
            self._failed(r, now, why)

    def _drain(self, now):
        while True:
            try:
                r, gen, ok, info, seconds = self.results.get_nowait()
            except queue.Empty:
                return
            if gen != r.gen or r.state == 'down' or r not in self.routes:
                continue
            r.probing = False
            if r.state == 'connecting':
                self._connect_result(r, now, ok, info, seconds)
            else:
                self._record(r, now, ok, info, seconds)

    def _connect_result(self, r, now, ok, info, seconds):
        r.attempt_probes += 1
        if not ok:
            r.last_info = info
            r.next_probe = now + CONNECT_POLL_S
            return
        log('tunnel: up via %s, %s' % (r.dest, info))
        r.state = 'up'
        r.fails_in_row, r.fail_since = 0, None
        r.burst_left, r.burst_ok, r.burst_n, r.burst_fail = self.samples, 0, 0, None
        if self.current is None:
            self._switch(now, r, 'recovered' if self.ever_up else 'startup', cooldown=False)
        self._record(r, now, ok, info, seconds)

    def _record(self, r, now, ok, info, seconds):
        if r.wake_check:
            r.wake_check = False
            if not ok:  # the connection died during sleep: restart it now, not after ServerAlive or the gates
                self._stop_child(r)
                self._dropped(r, now, 'no answer after wake-up (%s)' % info, count=False)
                self.retry_now, self.next_measure = True, now
                return
        r.results.append((now, ok, seconds))
        self._prune(r, now)
        r.last_ok = ok
        if not ok:
            r.burst_fail = info
        if r.burst_left > 0:
            r.burst_n += 1
            r.burst_ok += 1 if ok else 0
            r.burst_left -= 1
        else:
            r.burst_n, r.burst_ok, r.burst_fail = 1, 1 if ok else 0, None if ok else info
        if r.burst_left > 0:
            r.next_probe = now + self.sample_gap_s
        else:
            self._log_measure(r, now, last=r.burst_fail)
            r.burst_n = r.burst_ok = 0
            r.burst_fail = None
            r.next_probe = now + self.check_interval_s if r is self.current else None
        if ok:
            r.fails_in_row, r.fail_since = 0, None
            if self.current is None:
                self._switch(now, r, 'recovered', cooldown=False)
            return
        self._failed(r, now, info)

    def _failed(self, r, now, info):
        """A failed probe (or relayed connection) of an up route: a drop after unhealthy_restart_s; on the route
        in use, failover_after_fails in a row switch at once to the best other route."""
        r.fails_in_row += 1
        r.last_info = info
        if r.fail_since is None:
            r.fail_since = now
        if now - r.fail_since >= self.unhealthy_restart_s:
            self._stop_child(r)
            self._dropped(r, now, 'unhealthy for %.0f s (%s)' % (self.unhealthy_restart_s, info))
            return
        if r is self.current and r.fails_in_row >= self.failover_after_fails:
            ranked = self._rank(now, exclude=r)
            if ranked:
                self._switch(now, ranked[0], 'failover (%d failed probes on %s: %s)' % (r.fails_in_row, r.dest,
                                                                                        info), cooldown=True)

    def _log_measure(self, r, now, attempt=False, last=None):
        self._prune(r, now)
        ok, n = (0, r.attempt_probes) if attempt else (r.burst_ok, r.burst_n)
        median = self._median_ms(r)
        failed = sum(1 for _, good, _ in r.results if not good)
        line = 'tunnel: measure %s port %d ok %d/%d median %s fail %d/%d drops %d cost %s %s' % (
            r.dest, r.port, ok, n, 'n/a' if median is None else ms(median), failed, len(r.results), len(r.drops),
            ms(self._cost(r, now)), 'current' if r is self.current else 'standby')
        self._say(line + (' last: %s' % last if last else ''))

    # ------------------------------------------------------------------ choosing
    def _switch(self, now, to, reason, cooldown):
        old = self.current
        name = old.dest if old is not None else 'none'
        if old is None:
            self.quiet = False
            if self.unlogged:
                log('tunnel: %s not logged (rounds %d-%d)' % (self._same(len(self.unlogged)), self.unlogged[0],
                                                              self.unlogged[-1]))
        log('tunnel: switch %s -> %s: %s' % (name, to.dest, reason))
        self.current, self.switched_at, self.better = to, now, None
        if not reason.startswith('startup'):  # the start's own picks are not switches in the phase
            self.last_switch = '%s from %s: %s' % (time.strftime('%H:%M:%S', time.localtime(self.wall())), name,
                                                   reason)
        if cooldown:
            self.cooldown_until = now + self.switch_cooldown_s
        if to.burst_left == 0 and not to.probing and to.next_probe is None:
            to.next_probe = now + self.check_interval_s
        if old is not None and old.up and old.burst_left == 0 and not old.probing:
            old.next_probe = None
        if old is None:
            if self.ever_up:  # the end of a time without any route: one margin-only pick once all are measured
                self.settling, self.settle_kind = True, 'recovered'
            self.ever_up = True
            self.round, self.reconnecting, self.in_round, self.next_round_at = 1, False, False, None
            self.failures, self.last_failures, self.last_sig, self.repeat = [], [], None, False
            self.logged_at, self.unlogged, self.counted = None, [], 0
            self.next_measure = now + self.measure_interval_s

    def _lost_current(self, now):
        old = self.current
        why = '%s dropped: %s' % (old.dest, old.down_why)
        ranked = self._rank(now, exclude=old) or self._rank(now, exclude=old, need_last_ok=False)
        if ranked:
            self._switch(now, ranked[0], 'failover (%s)' % why, cooldown=True)
            return
        self.current, self.better = None, None
        self.round, self.reconnecting, self.in_round = 1, True, False
        wait = 0.0 if self.retry_now else self.restart_delay_s
        self.retry_now = False
        self.next_round_at = now + wait
        self.wait_note = 'dropped (%s)' % why
        log('tunnel: no route left (%s); round 1%s in %.1f s' % (why, self._limit(), wait))

    def _decide(self, now):
        cur = self.current
        if cur is None:
            return
        if self.settling:
            if any(r.state == 'connecting' or (r.up and r.burst_left > 0) for r in self.routes):
                return
            self.settling = False
            ranked = self._rank(now, exclude=cur)
            if ranked and self._beats(now, ranked[0], cur):
                self._switch(now, ranked[0], '%s best (cost %s < %s)' % (
                    self.settle_kind, ms(self._cost(ranked[0], now)), ms(self._cost(cur, now))), cooldown=False)
            return
        ranked = self._rank(now, exclude=cur)
        if not ranked or not self._beats(now, ranked[0], cur):
            self.better = None
            return
        best = ranked[0]
        if self.better is None or self.better[0] is not best:
            self.better = (best, now)
        if now - self.better[1] >= self.switch_hold_s and now >= self.cooldown_until:
            self._switch(now, best, 'better (cost %s < %s for %g s)' % (
                ms(self._cost(best, now)), ms(self._cost(cur, now)), self.switch_hold_s), cooldown=True)

    def _sync_relay(self, now):
        cur = self.current
        if cur is None:
            self.choice = ()
            if self.relay.listening:
                self.relay.close()
            return
        self.choice = tuple([cur.port] + [r.port for r in self._rank(now, exclude=cur)])
        if not self.relay.listening:
            try:
                self.relay.open()
            except OSError as exc:
                self._die('%s:%d is already in use by another program; set field.local_port to a free port (%s)'
                          % (LOOPBACK, self.local_port, exc))

    # ------------------------------------------------------------------ rounds
    def _die(self, text):
        """End the applet: no route. Exit 78, so the host does not restart it and shows this line as the reason;
        close() repeats the line last."""
        self.fatal = 'tunnel: ' + text
        self.phase = 'no route: ' + text
        log(self.fatal)
        raise SystemExit(EXIT_CONFIG)

    def _begin_round(self, now):
        taken, self.local_port_taken = self.local_port_taken, False
        if taken or (not self.relay.listening and port_open(self.local_port)):
            self._die('%s:%d is already in use by another program; set field.local_port to a free port'
                      % (LOOPBACK, self.local_port))
        self._sync_routes(now)
        self.net_sig, self.next_net_check = self.net_probe(), now + self.net_check_interval_s
        self.retry_now = False
        self.failures, self.next_round_at, self.in_round = [], None, True
        self.quiet = self.repeat and self.logged_at is not None and now - self.logged_at < REPEAT_LOG_S
        for r in self.routes:
            if r.state == 'down':
                self._spawn(r, now)

    def _say(self, text):
        """A line of a round: left out while the round repeats the last one within REPEAT_LOG_S."""
        if not self.quiet:
            log(text)

    def _limit(self):
        return '/%d' % self.max_failed_rounds if self.max_failed_rounds else ''

    @staticmethod
    def _same(n):
        return '%d round%s with the same result' % (n, '' if n == 1 else 's')

    def _round_wait(self, done):
        """The wait after ``done`` failed rounds in a row: restart_delay_s, doubled per round, capped."""
        return min(self.restart_delay_max_s, self.restart_delay_s * 2 ** min(done - 1, 30))

    def _round_failed(self, now):
        tried = '; '.join(self.failures)
        if self.woke_at is None or now - self.woke_at >= SHORT_WAKE_S:
            self.counted += 1
        if self.max_failed_rounds and self.counted >= self.max_failed_rounds:
            self.quiet = False
            self._die('no ssh target of machine %s connects in %d round%s in a row (last round, tried in order: %s)'
                      % (self.machine, self.counted, '' if self.counted == 1 else 's', tried))
        done = self.round
        wait = self._round_wait(done)
        sig = tuple(sorted(self.failures))
        repeat = sig == self.last_sig
        self.round += 1
        self.reconnecting, self.in_round = True, False
        self.next_round_at = now + wait
        self.last_failures, self.last_sig, self.repeat = list(self.failures), sig, repeat
        self.wait_note = 'round %d: no target connects (%s)' % (done, tried)
        if self.quiet and repeat:
            self.unlogged.append(done)
            return
        if not self.quiet:
            self.logged_at = now
        self.quiet = False
        note = '; %s not logged' % self._same(len(self.unlogged)) if self.unlogged else ''
        self.unlogged = []
        log('tunnel: round %d%s: no target connects (tried in order: %s); round %d in %.1f s%s'
            % (done, self._limit(), tried, self.round, wait, note))

    def _restart_rounds(self, now):
        """No route: stop the routes still connecting (not failures) and start round 1 now; the wait and the
        round count start over."""
        for r in self.routes:
            if r.state == 'connecting':
                self._stop_child(r)
                self._gone(r)
        self.round, self.in_round, self.failures, self.next_round_at = 1, False, [], now

    def _wake(self, now):
        """A clock jump of wake_jump_s or more since the last step: the machine slept (or the loop was held)."""
        wall = self.wall()
        tick, self.last_tick = self.last_tick, None
        if tick is not None:
            last_wall, last_now, delay = tick
            jump = max((wall - last_wall) - (now - last_now), (now - last_now) - delay)
            if jump >= self.wake_jump_s:
                self.quiet = False
                self.woke_at, self.counted = now, 0
                log('tunnel: woke up (clock jump %.0f s); probing every route now' % jump)
                if self.current is None:
                    self._restart_rounds(now)
                else:
                    for r in self.routes:
                        if r.up:  # a probe in flight from before the sleep is dropped (new gen)
                            r.gen += 1
                            r.wake_check, r.probing, r.next_probe = True, False, now
                    self.next_measure = now
        return wall

    def _net_watch(self, now):
        """No route: a new network (default routes changed to an available one) starts a round at once."""
        if now < self.next_net_check:
            return
        self.next_net_check = now + self.net_check_interval_s
        sig, old = self.net_probe(), self.net_sig
        self.net_sig = sig
        if old is None or any(old) or not any(sig) or (self.next_round_at is None and not self.in_round):
            return  # only "no network -> network" starts a round at once; other changes follow the wait
        name = lambda x: '/'.join(a for a in x if a) if x and any(x) else 'none'
        log('tunnel: network changed (%s -> %s); round now' % (name(old), name(sig)))
        self._restart_rounds(now)

    def _rounds(self, now):
        if self.next_round_at is not None:
            if now >= self.next_round_at:
                self._begin_round(now)
            return
        if self.in_round and all(r.state == 'down' for r in self.routes):
            self._round_failed(now)

    def _cycle(self, now):
        if now < self.next_measure:
            return
        self.next_measure = now + self.measure_interval_s
        self._sync_routes(now)
        if self.current is not None and not self.current.up:
            return
        for r in self.routes:
            if r.state == 'down':
                self._spawn(r, now)
            elif r.up and r is not self.current and r.burst_left == 0:
                r.burst_left, r.burst_ok, r.burst_n, r.burst_fail = self.samples, 0, 0, None
                if not r.probing:
                    r.next_probe = now

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Probe local_port first; the first step starts round 1, or exits 78 before any ssh when another
        program holds local_port."""
        now = self.clock()
        self.local_port_taken = port_open(self.local_port)
        self.relay = make_relay(self.local_port, self._choose, self._on_route_refused)
        self.next_measure = now + self.measure_interval_s
        self.next_round_at = now

    def _poll_children(self, now):
        for r in self.routes:
            if r.child is None or r.child.poll() is None:
                continue
            why = 'ssh exited with %s' % r.child.returncode
            r.child = None
            if r.up:
                self._dropped(r, now, why)
            else:
                self._attempt_failed(r, now, why)

    def _deadlines(self, now):
        for r in self.routes:
            if r.state == 'connecting' and now >= r.attempt_deadline:
                self._stop_child(r)
                self._attempt_failed(r, now, 'no healthy forward within %.0f s (%s)' % (self.attempt_timeout_s,
                                                                                       r.last_info))

    def _check_current(self, now):
        if self.current is not None and not self.current.up:
            self._lost_current(now)

    def step(self):
        now = self.clock()
        wall = self._wake(now)
        self._drain(now)
        self._drain_refused(now)
        self._poll_children(now)
        self._deadlines(now)
        self._check_current(now)
        if self.current is None:
            self._net_watch(now)
            self._rounds(now)
        else:
            self._cycle(now)
            self._check_current(now)
        self._launch_probes(now)
        self._drain(now)
        self._check_current(now)
        self._decide(now)
        self._sync_relay(now)
        self.phase = self._phase(now)
        delay = self._delay(now)
        self.last_tick = (wall, now, delay)
        return delay

    def _delay(self, now):
        events = [now + self.check_interval_s, now + self.net_check_interval_s]
        for r in self.routes:
            if r.probing:
                events.append(now + PROBE_WAIT_S)
            elif r.next_probe is not None:
                events.append(r.next_probe)
            if r.state == 'connecting':
                events.append(r.attempt_deadline)
        if self.current is not None:
            events.append(self.next_measure)
        if self.next_round_at is not None:
            events.append(self.next_round_at)
        if self.better is not None:
            events.append(max(self.better[1] + self.switch_hold_s, self.cooldown_until))
        return max(0.05, min(events) - now)

    def _phase(self, now):
        cur = self.current
        if cur is not None:
            self._prune(cur, now)
            median = self._median_ms(cur)
            failed = sum(1 for _, ok, _ in cur.results if not ok)
            others = []
            for r in self.routes:
                if r is cur:
                    continue
                if r.state != 'up':
                    others.append('%s %s' % (r.dest, 'connecting' if r.state == 'connecting' else 'down'))
                else:
                    cost = self._cost(r, now)
                    others.append('%s %s' % (r.dest, 'unfit' if math.isinf(cost) else ms(self._median_ms(r))))
            return 'up: via %s %s (fail %d/%d, drops %d) | switched %s | standby: %s' % (
                cur.dest, 'n/a' if median is None else ms(median), failed, len(cur.results), len(cur.drops),
                self.last_switch, ', '.join(others) or 'none')
        word = 'reconnecting' if self.reconnecting else 'connecting'
        if self.next_round_at is not None:
            return '%s: round %d%s in %.0f s; %s' % (word, self.round, self._limit(),
                                                     max(0.0, self.next_round_at - now), self.wait_note)
        trying = [self._label(r) for r in self.routes if r.state == 'connecting']
        last = ('; round %d: no target connects (%s)' % (self.round - 1, '; '.join(self.last_failures))
                if self.last_failures else '')
        return '%s: round %d%s, trying %s%s' % (word, self.round, self._limit(), ', '.join(trying) or 'none', last)

    def close(self):
        self.quiet = False
        for r in self.routes:
            self._stop_child(r)
        for child, _ in self.lingering:
            if child.poll() is None:
                child.kill()
        if self.relay is not None:
            self.relay.shutdown()
        ports = [self.local_port] + [r.port for r in self.routes]
        deadline = time.monotonic() + self.stop_wait_s
        while any(port_open(p) for p in ports) and time.monotonic() < deadline:
            time.sleep(0.1)
        busy = [p for p in ports if port_open(p)]
        if busy:
            log('tunnel: %s still accept connections after the stop (not this applet\'s children)'
                % ', '.join('%s:%d' % (LOOPBACK, p) for p in busy))
        else:
            log('tunnel: %s:%d closed; pid %d exits' % (LOOPBACK, self.local_port, os.getpid()))
        if self.fatal:
            log(self.fatal)  # the host shows the last stderr line as the reason


if __name__ == '__main__':
    main(Tunnel, 'field')
