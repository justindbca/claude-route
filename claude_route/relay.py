"""The field applet's loopback relay: ``127.0.0.1:<local_port>`` -> the route_port of the route in use.

Each route has its own ssh child that forwards a loopback ``route_port`` to the home proxy. Sessions only ever
connect to ``local_port``; this relay accepts there and forwards each connection to a route_port.

- ``LoopbackRelay(port, choose, on_route_refused=None)``. It binds ``127.0.0.1`` only.
- ``choose()`` returns the candidate route_ports in order of preference (the route in use first; empty = no
  route). It is called once per accepted connection: that snapshot decides where the connection goes for its
  whole life. Changing what ``choose()`` returns moves new connections only; existing ones stay on their route
  until they end, or until that route's ssh goes away and closes them.
- The first candidate refused: the second one is tried, once. Both refused, or no candidate: the client
  connection is closed. ``on_route_refused(port)`` (optional) is called for every candidate that refused.
- Half-close is passed through: EOF from one side shuts down only the write direction towards the other side;
  the other direction keeps copying until its own EOF (a CONNECT tunnel may half-close and still read).
- ``open()`` starts listening (``OSError`` when the port is in use). ``close()`` stops listening: the port
  refuses connections and existing connections are left alone; ``open()`` may be called again.
  ``shutdown()`` stops listening, ends every connection and returns after every relay thread has ended.
  ``listening`` tells whether the port is being listened on.
"""
import socket
import threading

from claude_route import LOOPBACK

CONNECT_TIMEOUT_S = 3.0  # connect to a route_port on the loopback; refused is immediate, this bounds the rest
ACCEPT_POLL_S = 0.1      # how often the accept thread checks for close() / shutdown()
THREAD_PREFIX = 'claude-route-relay'


class LoopbackRelay:
    def __init__(self, port, choose, on_route_refused=None):
        self.port = port
        self.choose = choose
        self.on_route_refused = on_route_refused
        self._lock = threading.Lock()
        self._listener = None      # the listening socket while open
        self._accept_thread = None
        self._stop_accept = None   # threading.Event of the current accept thread
        self._conns = set()        # _Conn objects not yet finished
        self._shutting_down = False

    @property
    def listening(self):
        return self._listener is not None

    # ------------------------------------------------------------------ listener
    def open(self):
        """Listen on 127.0.0.1:<port>. OSError when another program already accepts connections there."""
        with self._lock:
            if self._listener is not None:
                return
            if _accepts(self.port):  # also catches a listener on the wildcard address, which bind would allow
                raise OSError('%s:%d is already in use by another program' % (LOOPBACK, self.port))
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # rebind past TIME_WAIT after close()
                srv.bind((LOOPBACK, self.port))
                if srv.getsockname()[0] != LOOPBACK:  # defence in depth: never serve on another address
                    raise OSError('refusing to listen on %s' % (srv.getsockname(),))
                srv.listen(128)
                srv.settimeout(ACCEPT_POLL_S)
            except OSError:
                srv.close()
                raise
            self._shutting_down = False
            self._listener = srv
            self._stop_accept = threading.Event()
            self._accept_thread = threading.Thread(
                target=self._accept_loop, args=(srv, self._stop_accept),
                name='%s-accept-%d' % (THREAD_PREFIX, self.port), daemon=True)
            self._accept_thread.start()

    def close(self):
        """Stop listening; the port refuses connections when this returns. Existing connections go on."""
        with self._lock:
            thread, stop = self._accept_thread, self._stop_accept
            self._accept_thread = self._stop_accept = None
        if thread is None:
            return
        stop.set()
        thread.join()  # the accept thread closes the listener itself before it ends

    def shutdown(self):
        """Stop listening, end every connection, return after every relay thread has ended."""
        self.close()
        with self._lock:
            self._shutting_down = True
            conns = list(self._conns)
        for conn in conns:
            conn.abort()
        for conn in conns:
            conn.join()

    def _accept_loop(self, srv, stop):
        try:
            while not stop.is_set():
                try:
                    client, _ = srv.accept()
                except socket.timeout:
                    continue
                client.settimeout(None)
                with self._lock:
                    if self._shutting_down:
                        client.close()
                        continue
                    conn = _Conn(self, client)
                    self._conns.add(conn)
                conn.start()
        finally:
            srv.close()
            with self._lock:
                if self._listener is srv:
                    self._listener = None

    # ------------------------------------------------------------------ connections
    def _connect(self):
        """A connected socket to the first candidate that accepts (at most two tried), or None."""
        for port in list(self.choose())[:2]:
            try:
                up = socket.create_connection((LOOPBACK, port), timeout=CONNECT_TIMEOUT_S)
            except OSError:
                if self.on_route_refused is not None:
                    self.on_route_refused(port)
                continue
            up.settimeout(None)
            return up
        return None

    def _finished(self, conn):
        with self._lock:
            self._conns.discard(conn)


class _Conn:
    """One client connection: connects to a route, then copies both ways in two threads."""

    def __init__(self, relay, client):
        self.relay = relay
        self.client = client
        self.upstream = None
        self.lock = threading.Lock()
        self.aborted = False
        self.thread = threading.Thread(target=self._run, name='%s-conn-%d' % (THREAD_PREFIX, relay.port),
                                       daemon=True)
        self.back = None

    def start(self):
        self.thread.start()

    def join(self):
        self.thread.join()

    def abort(self):
        """End both directions now (shutdown): blocked recv / sendall calls return at once."""
        with self.lock:
            self.aborted = True
            socks = [s for s in (self.client, self.upstream) if s is not None]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        try:
            up = self.relay._connect()
            if up is None:
                return
            with self.lock:
                if self.aborted:
                    up.close()
                    return
                self.upstream = up
            self.back = threading.Thread(target=self._copy, args=(up, self.client),
                                         name='%s-conn-%d' % (THREAD_PREFIX, self.relay.port), daemon=True)
            self.back.start()
            self._copy(self.client, up)
            self.back.join()
        finally:
            for s in (self.client, self.upstream):
                if s is not None:
                    s.close()
            self.relay._finished(self)

    def _copy(self, src, dst):
        """Copy src -> dst until src's EOF, then shut down only dst's write direction (half-close passed on);
        the other direction goes on until its own EOF. A reset or a broken pipe ends both directions."""
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            self.abort()
            return
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            self.abort()


def _accepts(port):
    try:
        with socket.create_connection((LOOPBACK, port), timeout=0.5):
            return True
    except OSError:
        return False
