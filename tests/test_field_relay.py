"""claude_route.relay.LoopbackRelay on real loopback sockets.

Each "route" is a local server on its own loopback port that stands in for the ssh forward of one route. The
echo routes answer every chunk with ``<name>:<chunk>``, so a client sees which route carries its connection.
"""
import socket
import threading
import time

import pytest

from claude_route.relay import LoopbackRelay

TIMEOUT = 5
BIG = b'x' * (1 << 20)  # larger than the socket buffers: the copy must keep running after the half-close


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def refused(port):
    try:
        socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
        return False
    except OSError:
        return True


def read_all(sock):
    out = b''
    while True:
        data = sock.recv(65536)
        if not data:
            return out
        out += data


def relay_threads():
    return [t for t in threading.enumerate() if t.name.startswith('claude-route-relay')]


def wait_for(cond, timeout=TIMEOUT):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, 'timed out'
        time.sleep(0.02)


class Route:
    """A server on 127.0.0.1:<port>. ``handler(conn, route)`` serves each accepted connection (default: echo
    each chunk with the route name as prefix)."""

    def __init__(self, name, handler=None):
        self.name = name.encode()
        self.handler = handler or Route.echo
        self.srv = socket.socket()
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.conns = []
        self.received = []
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn):
        try:
            self.handler(conn, self)
        except OSError:
            pass

    @staticmethod
    def echo(conn, route):
        while True:
            data = conn.recv(65536)
            if not data:
                conn.close()
                return
            conn.sendall(route.name + b':' + data)

    def close_all(self):
        """The route goes away: the listener and every accepted connection close (what ssh exiting does)."""
        self.srv.close()
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()


@pytest.fixture
def routes():
    made = []

    def make(name, handler=None):
        r = Route(name, handler)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close_all()


@pytest.fixture
def relay():
    """make(choose, **kw) -> an opened LoopbackRelay on a free port; every one is shut down afterwards."""
    made = []

    def make(choose, **kw):
        r = LoopbackRelay(free_port(), choose, **kw)
        r.open()
        made.append(r)
        return r

    yield make
    for r in made:
        r.shutdown()


def connect(relay):
    return socket.create_connection(('127.0.0.1', relay.port), timeout=TIMEOUT)


def ask(sock, data=b'ping'):
    sock.sendall(data)
    return sock.recv(65536)


# ---------------------------------------------------------------------- listener
def test_listens_on_loopback_only(relay):
    r = relay(lambda: [])
    assert r.listening
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))  # no packet is sent; this only picks the outgoing interface address
        address = probe.getsockname()[0]
    except OSError:
        address = None
    finally:
        probe.close()
    if not address or address.startswith('127.'):
        pytest.skip('no non-loopback IPv4 address on this machine')
    with pytest.raises(OSError):
        socket.create_connection((address, r.port), timeout=1).close()


def test_closed_before_open_and_open_on_a_used_port_raises():
    port = free_port()
    r = LoopbackRelay(port, lambda: [])
    assert not r.listening and refused(port)
    other = socket.socket()
    other.bind(('127.0.0.1', port))
    other.listen()
    try:
        with pytest.raises(OSError):
            r.open()
        assert not r.listening
    finally:
        other.close()
    r.shutdown()


# ---------------------------------------------------------------------- where a new connection goes
def test_new_connection_follows_the_choose_snapshot(relay, routes):
    a, b = routes('A'), routes('B')
    order = [a.port, b.port]
    calls = []
    r = relay(lambda: calls.append(1) or list(order))
    with connect(r) as c:
        assert ask(c) == b'A:ping'
        assert ask(c, b'again') == b'A:again'
    order[:] = [b.port, a.port]
    with connect(r) as c:
        assert ask(c) == b'B:ping'
    assert len(calls) == 2  # once per accepted connection, not per chunk


def test_switch_keeps_old_connection_both_ways_and_sends_new_ones_to_the_new_port(relay, routes):
    a, b = routes('A'), routes('B')
    order = [a.port]
    r = relay(lambda: list(order))
    old = connect(r)
    try:
        assert ask(old) == b'A:ping'
        order[:] = [b.port, a.port]
        with connect(r) as new:
            assert ask(new) == b'B:ping'
        assert ask(old, b'still') == b'A:still'
        assert len(a.conns) == 1 and len(b.conns) == 1
    finally:
        old.close()


def test_upstream_close_ends_only_its_connections(relay, routes):
    a, b = routes('A'), routes('B')
    order = [a.port]
    r = relay(lambda: list(order))
    on_a = connect(r)
    assert ask(on_a) == b'A:ping'  # the relay accepted it (and took its snapshot) before the change below
    order[:] = [b.port]
    on_b = connect(r)
    try:
        assert ask(on_b) == b'B:ping'
        a.close_all()
        assert on_a.recv(65536) == b''  # the old route went away: its connection ends
        assert ask(on_b, b'alive') == b'B:alive'
        with connect(r) as c:
            assert ask(c) == b'B:ping'
    finally:
        on_a.close()
        on_b.close()


def test_refused_first_port_tries_the_second_once(relay, routes):
    b, c = routes('B'), routes('C')
    dead = free_port()
    refused_ports = []
    r = relay(lambda: [dead, b.port, c.port], on_route_refused=refused_ports.append)
    with connect(r) as s:
        assert ask(s) == b'B:ping'
    assert refused_ports == [dead] and c.conns == []


def test_first_two_refused_closes_the_client_and_never_tries_a_third(relay, routes):
    c = routes('C')
    dead1, dead2 = free_port(), free_port()
    refused_ports = []
    r = relay(lambda: [dead1, dead2, c.port], on_route_refused=refused_ports.append)
    with connect(r) as s:
        try:
            assert s.recv(65536) == b''
        except ConnectionResetError:
            pass
    assert refused_ports == [dead1, dead2] and c.conns == []
    assert r.listening  # one refused client does not stop the relay


def test_refused_without_callback_still_closes_the_client(relay):
    r = relay(lambda: [free_port()])
    with connect(r) as s:
        try:
            assert s.recv(65536) == b''
        except ConnectionResetError:
            pass


def test_no_route_closes_the_client(relay, routes):
    a = routes('A')
    order = []
    r = relay(lambda: list(order))
    with connect(r) as s:
        try:
            assert s.recv(65536) == b''
        except ConnectionResetError:
            pass
    order[:] = [a.port]
    with connect(r) as s:
        assert ask(s) == b'A:ping'
    assert len(a.conns) == 1


# ---------------------------------------------------------------------- close / shutdown
def test_close_refuses_new_connections_and_keeps_existing_ones(relay, routes):
    a = routes('A')
    r = relay(lambda: [a.port])
    old = connect(r)
    try:
        assert ask(old) == b'A:ping'
        r.close()
        assert not r.listening and refused(r.port)
        assert ask(old, b'after close') == b'A:after close'
        r.open()  # every route down, then one comes back: the relay listens again on the same port
        assert r.listening
        with connect(r) as s:
            assert ask(s) == b'A:ping'
        assert ask(old, b'still') == b'A:still'
    finally:
        old.close()


def test_shutdown_ends_connections_listener_and_threads(routes):
    a = routes('A')
    before = set(relay_threads())
    r = LoopbackRelay(free_port(), lambda: [a.port])
    r.open()
    clients = [socket.create_connection(('127.0.0.1', r.port), timeout=TIMEOUT) for _ in range(3)]
    try:
        for c in clients:
            assert ask(c) == b'A:ping'
        assert set(relay_threads()) - before
        r.shutdown()
        assert not r.listening and refused(r.port)
        for c in clients:
            try:
                assert c.recv(65536) == b''
            except ConnectionResetError:
                pass
        assert set(relay_threads()) - before == set()  # shutdown() returns after every thread ended
        r.shutdown()  # a second call is harmless
    finally:
        for c in clients:
            c.close()


# ---------------------------------------------------------------------- half-close
def reply_after_eof(conn, route):
    """Read until the client's EOF, then send a reply larger than the socket buffers, then close."""
    got = read_all(conn)
    route.received.append(got)
    conn.sendall(b'got %d;' % len(got) + BIG)
    conn.close()
    route.done.set()


def test_client_half_close_reaches_upstream_and_the_reply_still_flows(relay, routes):
    up = routes('U', reply_after_eof)
    r = relay(lambda: [up.port])
    with connect(r) as c:
        c.sendall(b'request body')
        c.shutdown(socket.SHUT_WR)
        assert read_all(c) == b'got 12;' + BIG
    assert up.done.wait(TIMEOUT) and up.received == [b'request body']


def banner_then_read(conn, route):
    """Send a banner and half-close (the upstream's write side ends), then keep reading until EOF."""
    conn.sendall(b'banner')
    conn.shutdown(socket.SHUT_WR)
    route.received.append(read_all(conn))
    conn.close()
    route.done.set()


def test_upstream_half_close_reaches_client_and_the_client_can_still_send(relay, routes):
    up = routes('U', banner_then_read)
    r = relay(lambda: [up.port])
    with connect(r) as c:
        assert read_all(c) == b'banner'  # EOF from the upstream's half-close
        c.sendall(BIG)  # the other direction still flows to the end
        c.shutdown(socket.SHUT_WR)
        assert up.done.wait(TIMEOUT)
        assert up.received == [BIG]
    # both directions ended: the connection's copy threads end by themselves (only the accept thread remains)
    wait_for(lambda: [t for t in relay_threads() if '-accept-' not in t.name] == [])
