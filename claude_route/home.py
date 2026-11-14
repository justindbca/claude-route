"""applet-homeproxy-1: a minimal HTTP CONNECT proxy on 127.0.0.1 of the home machine.

- Listens on ``127.0.0.1:<port>`` only. There is no bind parameter; a ``bind`` / ``host`` / ``listen`` key in
  the parameter block is a config error (exit 78).
- ``CONNECT host:port`` to an allowed port (default 443) opens a TCP tunnel from this machine, so the far
  side sees this machine's public address.
- ``GET /__claude_route/health`` answers 200 with ``{"service": "claude-route-homeproxy", ...}``. A port that
  merely accepts connections is not this proxy; health checks compare the service name.
- Any other request is refused (405). Plain-HTTP forwarding is not offered.
- SIGTERM: the listener and open tunnels are closed, exit 0.

Run under a fleet (``applets:`` row in teams/home.yaml) or standalone:
``python -m claude_route.home --params claude_route.yaml``.
"""
import http.client
import json
import os
import select
import socket
import socketserver
import threading
import time

from claude_route import (HEALTH_PATH, LOOPBACK, SERVICE, ConfigError, __version__, check_port, check_seconds,
                          log, main, settings)

DEFAULTS = {
    'port': 17891,             # loopback port of the proxy
    'allow_ports': [443],      # CONNECT target ports
    'connect_timeout_s': 10,   # outbound TCP connect
    'idle_timeout_s': 900,     # a tunnel with no traffic either way this long is closed
    'check_interval_s': 10,    # self health check
}
REFUSED = {k: 'the proxy always binds 127.0.0.1; no other address can be configured'
           for k in ('bind', 'host', 'listen', 'address')}
HEAD_MAX = 16384               # request head size limit
HEAD_TIMEOUT_S = 10            # time a client has to send its request head
FAILS_BEFORE_CRASH = 3         # failed self checks in a row before the process exits 1 (the host restarts it)


def health_ok(port, timeout=3.0, host=LOOPBACK):
    """(True, document) when ``http://host:port/__claude_route/health`` answers 200 with this service's name;
    (False, reason) otherwise."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request('GET', HEALTH_PATH, headers={'Connection': 'close'})
        resp = conn.getresponse()
        body = resp.read(4096)
    except (OSError, http.client.HTTPException) as exc:
        return False, 'no answer: %s' % exc
    finally:
        conn.close()
    if resp.status != 200:
        return False, 'status %s' % resp.status
    try:
        doc = json.loads(body)
    except ValueError:
        return False, 'not JSON'
    if not isinstance(doc, dict) or doc.get('service') != SERVICE:
        return False, 'wrong service %r' % (doc.get('service') if isinstance(doc, dict) else doc)
    return True, doc


class LoopbackServer(socketserver.ThreadingTCPServer):
    """A threading TCP server that can only bind 127.0.0.1."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, handler, proxy):
        self.proxy = proxy
        super().__init__((LOOPBACK, port), handler)
        if self.server_address[0] != LOOPBACK:  # defence in depth: never serve on another address
            self.server_close()
            raise RuntimeError('refusing to serve on %s' % (self.server_address,))


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(HEAD_TIMEOUT_S)
        proxy = self.server.proxy
        proxy.track(self.request, True)
        try:
            head, rest = read_head(self.request)
            if head is None:
                return
            proxy.serve(self.request, head, rest)
        except OSError:
            pass
        finally:
            proxy.track(self.request, False)


def read_head(sock):
    """(request head bytes without the blank line, bytes after it) or (None, b'') on EOF / oversize."""
    buf = b''
    while b'\r\n\r\n' not in buf:
        if len(buf) > HEAD_MAX:
            send_status(sock, 431, 'Request Header Fields Too Large')
            return None, b''
        chunk = sock.recv(4096)
        if not chunk:
            return None, b''
        buf += chunk
    head, _, rest = buf.partition(b'\r\n\r\n')
    return head, rest


def send_status(sock, code, reason, body=b'', ctype='text/plain'):
    head = 'HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (
        code, reason, ctype, len(body))
    try:
        sock.sendall(head.encode('ascii') + body)
    except OSError:
        pass


def relay(a, b, idle_s):
    """Copy bytes both ways until both sides closed, or nothing moved for ``idle_s``."""
    a.settimeout(None)
    b.settimeout(None)
    peer = {a: b, b: a}
    open_ = [a, b]
    while open_:
        ready, _, _ = select.select(open_, [], [], idle_s)
        if not ready:
            return
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                open_.remove(s)
                try:
                    peer[s].shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                continue
            try:
                peer[s].sendall(data)
            except OSError:
                return


class HomeProxy:
    def __init__(self, port=17891, allow_ports=(443,), connect_timeout_s=10.0, idle_timeout_s=900.0,
                 check_interval_s=10.0):
        self.port = port
        self.allow_ports = frozenset(allow_ports)
        self.connect_timeout_s = connect_timeout_s
        self.idle_timeout_s = idle_timeout_s
        self.check_interval_s = check_interval_s
        self.server = None
        self.thread = None
        self.started_at = None
        self.phase = 'starting'
        self.tunnels = 0
        self.fails = 0
        self._socks = set()
        self._lock = threading.Lock()

    @classmethod
    def from_block(cls, block):
        s = settings(block, DEFAULTS, REFUSED)
        ports = s['allow_ports']
        if not (isinstance(ports, list) and ports and all(type(p) is int and 1 <= p <= 65535 for p in ports)):
            raise ConfigError('allow_ports must be a non-empty list of port numbers, got %r' % (ports,))
        return cls(port=check_port('port', s['port']), allow_ports=ports,
                   connect_timeout_s=check_seconds('connect_timeout_s', s['connect_timeout_s'], 1, 120),
                   idle_timeout_s=check_seconds('idle_timeout_s', s['idle_timeout_s'], 1, 86400),
                   check_interval_s=check_seconds('check_interval_s', s['check_interval_s'], 0.1, 3600))

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self.server = LoopbackServer(self.port, Handler, self)
        self.port = self.server.server_address[1]  # port 0 in tests: the kernel's choice
        self.started_at = time.time()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.2},
                                       name='homeproxy', daemon=True)
        self.thread.start()
        self.phase = 'serving %s:%d' % (LOOPBACK, self.port)
        log('homeproxy: listening on %s:%d (pid %d)' % (LOOPBACK, self.port, os.getpid()))

    def step(self):
        ok, info = health_ok(self.port)
        if ok:
            self.fails = 0
            self.phase = 'serving %s:%d, %d tunnels open' % (LOOPBACK, self.port, len(self._socks))
        else:
            self.fails += 1
            self.phase = 'self check failed (%d): %s' % (self.fails, info)
            log('homeproxy: ' + self.phase)
            if self.fails >= FAILS_BEFORE_CRASH:
                raise RuntimeError('health endpoint failed %d times in a row' % self.fails)
        return self.check_interval_s

    def close(self):
        if self.server is None:
            return
        server, self.server = self.server, None
        server.shutdown()
        server.server_close()
        with self._lock:
            socks = list(self._socks)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        log('homeproxy: listener closed, %d connections shut' % len(socks))

    # ------------------------------------------------------------------ requests
    def track(self, sock, add):
        with self._lock:
            (self._socks.add if add else self._socks.discard)(sock)

    def health_doc(self):
        return {'service': SERVICE, 'version': __version__, 'pid': os.getpid(), 'port': self.port,
                'started_at': self.started_at, 'tunnels_served': self.tunnels}

    def serve(self, sock, head, rest):
        try:
            line = head.split(b'\r\n', 1)[0].decode('ascii')
            method, target, version = line.split(' ')
        except (UnicodeDecodeError, ValueError):
            return send_status(sock, 400, 'Bad Request')
        if method == 'GET' and target == HEALTH_PATH:
            body = json.dumps(self.health_doc()).encode()
            return send_status(sock, 200, 'OK', body, 'application/json')
        if method != 'CONNECT':
            return send_status(sock, 405, 'Method Not Allowed', b'CONNECT only\n')
        host, sep, port_text = target.rpartition(':')
        host = host[1:-1] if host.startswith('[') and host.endswith(']') else host
        if not (sep and host and port_text.isdigit()):
            return send_status(sock, 400, 'Bad Request')
        port = int(port_text)
        if port not in self.allow_ports:
            log('homeproxy: refused CONNECT %s:%d (port not allowed)' % (host, port))
            return send_status(sock, 403, 'Forbidden', b'port not allowed\n')
        try:
            upstream = socket.create_connection((host, port), timeout=self.connect_timeout_s)
        except OSError as exc:
            log('homeproxy: CONNECT %s:%d failed: %s' % (host, port, exc))
            return send_status(sock, 502, 'Bad Gateway')
        self.track(upstream, True)
        try:
            sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if rest:
                upstream.sendall(rest)
            with self._lock:
                self.tunnels += 1
            log('homeproxy: CONNECT %s:%d' % (host, port))
            relay(sock, upstream, self.idle_timeout_s)
        finally:
            self.track(upstream, False)
            upstream.close()


if __name__ == '__main__':
    main(HomeProxy, 'home')
