"""Recipe check (``workflows.claude-via-home.check``): does the recipe apply on this machine now?

Opens one direct HTTPS request to the Claude API host, ignoring every proxy variable. Exit 0 (the recipe
applies) when there is no usable answer: connect, TLS or read failure, or HTTP 403. Exit 1 when the API host
answers directly, so the route is not needed. Standard library only; runs with any python3.
"""
import http.client
import socket
import ssl
import sys

HOST = 'api.anthropic.com'
TIMEOUT_S = 8


def direct_status(host=HOST, timeout=TIMEOUT_S):
    """HTTP status of ``GET /`` straight to ``host:443`` (http.client never reads proxy variables)."""
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request('GET', '/', headers={'Connection': 'close'})
        return conn.getresponse().status
    finally:
        conn.close()


def main(probe=direct_status):
    try:
        status = probe()
    except (OSError, ssl.SSLError, socket.timeout, http.client.HTTPException) as exc:
        print('applies: no direct answer from %s (%s)' % (HOST, exc))
        return 0
    if status == 403:
        print('applies: %s answered 403 directly' % HOST)
        return 0
    print('does not apply: %s answered %d directly' % (HOST, status))
    return 1


if __name__ == '__main__':
    sys.exit(main())
